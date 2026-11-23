#include "compute_trade/app.hpp"
#include "compute_trade/workers.hpp"
#include "app_config.hpp"
#include "app_io.hpp"
#include "diagnostics.hpp"
#include "order_transport.hpp"

#include <simplx.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <future>
#include <iostream>
#include <optional>
#include <thread>

namespace compute_trade {
namespace {

using namespace std::chrono_literals;
using tredzone::Actor;
using tredzone::Engine;
using optiq::Frame;
using optiq::Message;
using optiq::Template;
using app_io::Socket;
using Clock = std::chrono::steady_clock;

constexpr std::size_t no_connection = SIZE_MAX;
constexpr std::size_t no_notice = SIZE_MAX;
constexpr std::size_t commands_per_turn = 32;

using order_transport::Phase;
using order_transport::Reply;
using order_transport::Slot;

template <typename T>
class Ring {
  std::vector<T> storage_;
  std::size_t head_{};
  std::size_t count_{};

  std::size_t wrap(std::size_t index) const {
    return index < storage_.size() ? index : index - storage_.size();
  }

public:
  explicit Ring(std::size_t capacity) : storage_(capacity) {}
  std::size_t size() const { return count_; }
  std::size_t available() const { return storage_.size() - count_; }
  bool empty() const { return count_ == 0; }
  T &at(std::size_t index) { return storage_[wrap(head_ + index)]; }
  const T &at(std::size_t index) const { return storage_[wrap(head_ + index)]; }
  T &front() { return at(0); }
  void push(const T &value) {
    if (!available())
      throw std::logic_error("exchange ring overflow");
    at(count_++) = value;
  }
  void pop() {
    head_ = wrap(head_ + 1);
    --count_;
  }
};

struct Session {
  SessionKey key;
  std::vector<std::array<char, 8>> firms;
  Ring<Reply> replay;
  std::size_t active{no_connection};
  std::size_t first_notice{no_notice};
  std::size_t last_notice{no_notice};
  std::uint32_t received{};
  std::uint32_t outbound{};
  std::uint32_t confirmed{};
  bool started{};
  bool disconnect_waiting{};
  std::optional<std::uint32_t> replay_through;

  Session(const SessionRule &rule, std::size_t capacity)
      : key(rule.key), firms(rule.firms), replay(capacity) {}
};

struct Connection {
  Slot &transport;
  bool open{};
  std::optional<std::size_t> session;
  std::optional<std::size_t> requested;
  bool recovering{};
  bool replay_complete_queued{};
  std::uint32_t replay_cursor{};
  std::uint32_t replay_frontier{};
  std::size_t replay_ordinary{};
  std::size_t replay_notice{no_notice};

  explicit Connection(Slot &slot) : transport(slot) {}
  void reset() {
    open = false;
    session.reset();
    requested.reset();
    recovering = false;
    replay_complete_queued = false;
  }
};

struct Sample {
  int thread_id{};
  int cpu{};
  std::uint64_t iteration{};

  Json json() const {
    return Json::object().set("thread_id", thread_id).set("cpu", cpu).set("iteration", iteration);
  }
};

struct Command {
  Message request{};
  std::size_t session{};
  std::uint64_t sequence{};
  std::uint64_t book_in_time{};
  Sample decode_started{};
  bool disconnect{};
  bool bad_sequence{};
  std::string_view reported_block;
};

struct NoticeSlot {
  enum class State { free, ready, offered, copied };
  NoticeRecord record{};
  std::size_t owner{};
  std::size_t next{no_notice};
  State state{State::free};
};

struct NoticeBatch {
  std::uint64_t id{};
  std::vector<std::size_t> slots;
};

struct Control {
  Socket fd;
  std::string input, output;
  std::size_t written{};
  std::optional<Json> waiting;
  Clock::time_point deadline{};

  explicit Control(Socket socket) : fd(std::move(socket)), deadline(Clock::now() + 4s) {}
};

struct AppState {
  std::shared_ptr<Workers> workers;
  order_transport::Transport transport;
  Diagnostics diag;
  std::atomic<bool> failed{};
  std::atomic<int> matcher_tid{-1};
  std::atomic<int> matcher_cpu{-1};
  std::atomic_flag recording_error{};
  std::array<char, 512> error{};

  AppState(const Config &config, bool diagnostics) : transport(config), diag(diagnostics) {}

  void fail(std::string_view message) noexcept {
    if (recording_error.test_and_set(std::memory_order_relaxed))
      return;
    std::copy_n(message.begin(), std::min(message.size(), error.size() - 1), error.begin());
    failed.store(true, std::memory_order_release);
  }
};

Json worker_row(std::string_view role, int cpu, int thread_id) {
  return Json::object().set("role", role).set("cpu", cpu).set("thread_id", thread_id);
}

Json placement(const AppState &state, bool role_aliases) {
  const auto [publisher, blue] = state.workers->placement();
  auto workers = Json::array();
  workers.push(worker_row("gateway_matcher", state.matcher_cpu.load(), state.matcher_tid.load()));
  workers.push(worker_row(publisher.role, publisher.cpu, publisher.thread_id));
  if (role_aliases) {
    workers.push(worker_row("statistics", blue.cpu, blue.thread_id));
    workers.push(worker_row("disconnect_replay", blue.cpu, blue.thread_id));
    workers.push(worker_row("order_transport", state.transport.cpu.load(), state.transport.tid.load()));
  } else {
    auto responsibilities = Json::array();
    responsibilities.push("statistics");
    responsibilities.push("disconnect_replay");
    responsibilities.push("order_transport");
    workers.push(worker_row(blue.role, blue.cpu, blue.thread_id)
                     .set("responsibilities", std::move(responsibilities)));
  }
  return Json::object().set("process_id", ::getpid()).set("workers", std::move(workers));
}

struct GatewayInit {
  const AppOptions &options;
  const Config &config;
  std::shared_ptr<AppState> state;
};

class Gateway : public Actor, public Actor::Callback {
  Limits limits_;
  std::shared_ptr<AppState> state_;
  std::vector<Session> sessions_;
  std::vector<Connection> connections_;
  std::vector<std::size_t> active_slots_;
  std::uint64_t accepted_connections_{};
  std::map<SessionKey, std::size_t> session_indices_;
  std::vector<Control> controls_;
  std::size_t input_start_{};

  app_io::UnixListener control_;
  Matcher<Actor::Allocator<std::byte>> matcher_;
  MatchPlan plan_;
  Ring<Command> commands_;
  Ring<std::size_t> pending_disconnects_;
  std::uint64_t next_command_{1};
  std::uint32_t next_execution_id_{1};
  std::size_t queue_peak_{};
  bool full_reported_{};
  bool process_commands_{true};
  std::optional<std::size_t> publication_cap_;
  std::vector<std::size_t> reply_demand_;

  std::vector<NoticeSlot> notices_;
  std::vector<std::size_t> free_notices_;
  Ring<std::size_t> ready_notices_;
  std::vector<NoticeRecord> batch_;
  std::vector<NoticeBatch> notice_batches_;
  std::size_t outstanding_{};
  std::uint64_t next_batch_{1};
  std::size_t live_reservations_{};
  std::size_t notice_peak_{};
  std::size_t handoff_peak_{};

  std::size_t notice_occupied() const { return notices_.size() - free_notices_.size(); }

  Sample sample() const {
    return {state_->matcher_tid.load(std::memory_order_relaxed),
            state_->matcher_cpu.load(std::memory_order_relaxed),
            getCorePerformanceCounters().getLoopTotalCount()};
  }

  Json with_session(std::size_t index) const {
    const auto &key = sessions_[index].key;
    return Json::object().set("logical_access_id", key.access).set("partition_id", key.partition);
  }

  Json with_command(const Command &command) const {
    auto row = with_session(command.session);
    row.set("command_sequence", command.sequence);
    if (command.disconnect)
      row.set("kind", "disconnect");
    else
      row.set("client_order_id", command.request.client_order_id)
          .set("book_in_time", command.book_in_time);
    return row;
  }

  void event(std::string_view name, Json row) { state_->diag.record(name, std::move(row)); }

  void update_peaks() {
    notice_peak_ = std::max(notice_peak_, live_reservations_ + notice_occupied());
    handoff_peak_ = std::max(handoff_peak_, outstanding_);
  }

  void enqueue(Command command) {
    if (next_command_ == UINT64_MAX)
      throw std::overflow_error("gateway command sequence exhausted");
    command.sequence = next_command_++;
    if (state_->diag.enabled())
      event("command_sequenced", with_command(command));
    commands_.push(command);
    queue_peak_ = std::max(queue_peak_, commands_.size());
  }

  void disconnect(std::size_t slot) {
    auto &connection = connections_[slot];
    if (connection.session) {
      auto &identity = sessions_[*connection.session];
      identity.active = no_connection;
      identity.disconnect_waiting = true;
      pending_disconnects_.push(*connection.session);
      sequence_disconnects();
    }
    connection.reset();
  }

  void close_connection(std::size_t slot) {
    disconnect(slot);
    connections_[slot].transport.close_requested.store(true, std::memory_order_release);
  }

  void sequence_disconnects() {
    while (!pending_disconnects_.empty() && commands_.available()) {
      enqueue(Command{.session = pending_disconnects_.front(), .disconnect = true,
                      .reported_block = {}});
      pending_disconnects_.pop();
    }
  }

  void append(Connection &connection, const Reply &message) {
    connection.transport.outbound.push(message);
  }

  void defer_session_reply(Connection &connection, const Message &message,
                           std::uint32_t sequence = 0, bool close = false) {
    if (!connection.transport.outbound.empty())
      throw std::logic_error("session message cannot replace pending output");
    connection.transport.session_message = message;
    append(connection, Reply{.sequence = sequence, .end_connection = close});
  }

  void reap_notices(Session &identity) {
    while (identity.first_notice != no_notice) {
      const auto slot = identity.first_notice;
      auto &notice = notices_[slot];
      if (notice.state != NoticeSlot::State::copied || notice.record.sequence > identity.confirmed)
        break;
      identity.first_notice = notice.next;
      state_->workers->release_notice(slot);
      notice.state = NoticeSlot::State::free;
      free_notices_.push_back(slot);
    }
    if (identity.first_notice == no_notice)
      identity.last_notice = no_notice;
  }

  void confirm(std::size_t owner, std::uint32_t through, bool record) {
    auto &identity = sessions_[owner];
    if (through > identity.outbound)
      return;
    identity.confirmed = std::max(identity.confirmed, through);
    while (!identity.replay.empty() && identity.replay.front().sequence <= identity.confirmed)
      identity.replay.pop();
    reap_notices(identity);
    if (record && state_->diag.enabled())
      event("private_confirmed", with_session(owner).set("sequence", through));
  }

  void attach(Connection &connection, std::size_t slot) {
    if (!connection.requested)
      return;
    const auto index = *connection.requested;
    auto &identity = sessions_[index];
    if (identity.active != no_connection || identity.disconnect_waiting) {
      const auto now = state_->transport.monotonic_ns.load(std::memory_order_acquire);
      if (std::chrono::nanoseconds(now - connection.transport.opened_ns) > 5s) {
        connection.requested.reset();
        defer_session_reply(connection, Message{.type = Template::LogonReject, .logon_reject_code = 2},
                            0, true);
      }
      return;
    }
    if (connection.replay_cursor > identity.outbound) {
      connection.requested.reset();
      defer_session_reply(connection, Message{.type = Template::LogonReject, .logon_reject_code = 2},
                          0, true);
      return;
    }

    connection.session = index;
    connection.transport.session.store(index, std::memory_order_release);
    connection.requested.reset();
    identity.active = slot;
    identity.started = true;
    connection.recovering = true;
    connection.replay_frontier = identity.outbound;
    connection.replay_cursor = std::max(connection.replay_cursor, identity.confirmed);
    confirm(index, connection.replay_cursor, false);
    // Disconnect has committed behind earlier commands and removed this session's
    // orders, so it has no resting orders that can produce fills. Intake waits
    // until ReplayComplete is sent. The ordinary ring stays fixed during replay.
    // Only confirmed notice links, which this cursor skips, can be reclaimed.
    connection.replay_ordinary = 0;
    connection.replay_notice = identity.first_notice;
    while (connection.replay_notice != no_notice &&
           notices_[connection.replay_notice].record.sequence <= connection.replay_cursor)
      connection.replay_notice = notices_[connection.replay_notice].next;
    defer_session_reply(connection, Message{.type = Template::LogonAck, .sequence = identity.received});
  }

  Message notice_message(const NoticeRecord &notice) const {
    Message result{};
    result.type = Template::Kill;
    result.sequence = notice.sequence;
    result.firm = notice.firm;
    result.order_id = notice.order_id;
    result.instrument = notice.instrument;
    result.kill_reason = 11;
    result.time = optiq::null_order_id;
    result.book_in_time = notice.book_in_time;
    return result;
  }

  void stage_replay(Connection &connection) {
    if (!connection.recovering || !connection.transport.outbound.empty())
      return;
    const auto owner = *connection.session;
    auto &identity = sessions_[owner];
    if (connection.replay_complete_queued) {
      connection.replay_complete_queued = false;
      connection.recovering = false;
      connection.transport.allow_intake.store(true, std::memory_order_release);
      if (state_->diag.enabled())
        event("replay_completed", with_session(owner).set("sequence", connection.replay_frontier));
      return;
    }
    if (connection.replay_cursor == connection.replay_frontier) {
      Message response{};
      response.type = Template::ReplayComplete;
      response.sequence = connection.replay_frontier;
      defer_session_reply(connection, response);
      connection.replay_complete_queued = true;
      return;
    }

    const Reply *ordinary = connection.replay_ordinary < identity.replay.size()
                                 ? &identity.replay.at(connection.replay_ordinary) : nullptr;
    const NoticeSlot *notice = connection.replay_notice != no_notice
                                   ? &notices_[connection.replay_notice] : nullptr;
    const auto ordinary_sequence = ordinary ? ordinary->sequence : connection.replay_frontier + 1;
    const auto notice_sequence = notice ? notice->record.sequence : connection.replay_frontier + 1;
    const auto first = std::min(ordinary_sequence, notice_sequence);

    const auto expected = connection.replay_cursor + 1;
    const auto available = identity.replay_through.value_or(connection.replay_frontier);
    if (expected > available)
      return;
    if (first > expected) {
      Message gap{};
      gap.type = Template::ReplayGap;
      gap.first_sequence = expected;
      gap.last_sequence = std::min(available, first - 1);
      defer_session_reply(connection, gap, gap.last_sequence);
      connection.replay_cursor = gap.last_sequence;
    } else if (ordinary && first == ordinary_sequence) {
      append(connection, *ordinary);
      connection.replay_cursor = first;
      ++connection.replay_ordinary;
    } else if (notice && notice->state == NoticeSlot::State::copied) {
      auto retained = state_->workers->retained_notice(notice->record.slot);
      if (!retained)
        throw std::logic_error("BlueZone notice missing");
      defer_session_reply(connection, notice_message(*retained), first);
      connection.replay_cursor = first;
      connection.replay_notice = notice->next;
    }
  }

  void logon(Connection &connection, const Message &request) {
    auto index = session_indices_.find({request.logical_access_id, request.partition_id});
    if (index == session_indices_.end()) {
      defer_session_reply(connection, Message{.type = Template::LogonReject, .logon_reject_code = 1},
                          0, true);
      return;
    }
    connection.requested = index->second;
    connection.replay_cursor = request.last_sequence;
    if (state_->diag.enabled())
      event("replay_requested", with_session(index->second)
                .set("last_processed", request.last_sequence)
                .set("resuming", sessions_[index->second].started));
  }

  bool handle(std::size_t slot, const Message &message, Sample decode_started,
              std::uint64_t book_in_time) {
    auto &connection = connections_[slot];
    if (!connection.session) {
      if (message.type != Template::Logon)
        return false;
      connection.transport.allow_intake.store(false, std::memory_order_release);
      logon(connection, message);
      return true;
    }

    const auto index = *connection.session;
    auto &identity = sessions_[index];
    if (message.type == Template::Confirm) {
      confirm(index, message.sequence, true);
      return true;
    }
    if (message.type != Template::NewOrder && message.type != Template::CancelRequest &&
        message.type != Template::CancelReplace)
      return false;
    const bool bad_sequence = message.sequence != identity.received + 1 ||
                              message.sequence >= UINT32_MAX - 1;
    if (!bad_sequence)
      identity.received = message.sequence;
    enqueue(Command{.request = message, .session = index, .book_in_time = book_in_time,
                    .decode_started = decode_started, .bad_sequence = bad_sequence,
                    .reported_block = {}});
    return true;
  }

  std::size_t service_input(std::size_t slot, std::size_t budget) {
    auto &connection = connections_[slot];
    auto &transport = connection.transport;
    std::size_t frames = 0;
    while (frames < budget && commands_.available() && connection.open &&
           transport.allow_intake.load(std::memory_order_relaxed)) {
      const Frame *frame = transport.inbound.front();
      if (!frame)
        break;
      bool accepted = false;
      try {
        const auto book_in_time = state_->transport.epoch_ns.load(std::memory_order_acquire);
        const auto decode_started = state_->diag.enabled() ? sample() : Sample{};
        accepted = handle(slot, optiq::decode(frame->view()), decode_started, book_in_time);
      } catch (const std::invalid_argument &) {
        // Invalid SBE framing is connection-local.
      }
      transport.inbound.pop();
      ++frames;
      if (!accepted) {
        close_connection(slot);
        break;
      }
    }
    return frames;
  }

  std::size_t connection_capacity(std::size_t session) const {
    const auto slot = sessions_[session].active;
    return slot == no_connection ? SIZE_MAX : connections_[slot].transport.outbound.available();
  }

  std::uint32_t private_sequence(Session &identity) {
    if (identity.outbound >= UINT32_MAX - 2)
      throw std::overflow_error("private application sequence exhausted");
    return ++identity.outbound;
  }

  void send_private(std::size_t index, Message message) {
    auto &identity = sessions_[index];
    auto *connection = identity.active == no_connection ? nullptr : &connections_[identity.active];
    if (connection && connection->recovering)
      throw std::logic_error("live private reply attempted during replay");
    message.sequence = private_sequence(identity);
    Reply stored{optiq::encode(message), message.sequence};
    if (!identity.replay.available())
      identity.replay.pop();
    identity.replay.push(stored);
    if (connection)
      append(*connection, stored);
    if (state_->diag.enabled()) {
      event("private_reply_ready", with_session(index)
                .set("client_order_id", message.client_order_id)
                .set("order_id", message.order_id)
                .set("sequence", message.sequence)
                .set("template_id", static_cast<unsigned>(message.type)));
    }
  }

  Message response_for(const Message &request, const BookOrder &result,
                       std::string_view rejection, bool traded,
                       std::uint64_t book_in_time) {
    Message response{};
    response.client_order_id = request.client_order_id;
    response.original_client_order_id = request.original_client_order_id;
    response.firm = request.firm;
    response.instrument = request.instrument;
    response.side = request.side;
    response.time = optiq::null_order_id;
    response.book_in_time = book_in_time;
    if (!rejection.empty()) {
      response.type = Template::Reject;
      response.order_id = request.order_id;
      response.rejected_message_id = static_cast<std::uint16_t>(request.type);
      response.error_code = error_code(rejection);
      return response;
    }
    response.order_id = result.order_id;
    if (request.type == Template::CancelRequest) {
      response.type = Template::Kill;
      response.kill_reason = 1;
    } else {
      response.type = Template::Ack;
      response.ack_type = request.type == Template::NewOrder ? 0 : 1;
      response.ack_qualifiers = static_cast<std::uint8_t>(0x44 | (traded ? 0x80 : 0));
      response.price = result.price;
      response.quantity = request.quantity;
      response.priority = result.priority;
    }
    return response;
  }

  static std::uint16_t error_code(std::string_view reason) {
    if (reason == "unauthorised_firm")
      return 2;
    if (reason == "unknown_instrument")
      return 3;
    if (reason == "unknown_order" || reason == "wrong_owner")
      return 4;
    if (reason == "invalid_amendment" || reason == "incompatible_order")
      return 5;
    if (reason.ends_with("_capacity"))
      return 6;
    if (reason == "invalid_client_sequence")
      return 7;
    return 1;
  }

  void send_fill(const MatchFillParty &party, const MatchFill &fill, Side side,
                 std::uint64_t trade_time) {
    Message response{};
    response.type = Template::Fill;
    response.firm = party.firm;
    response.order_id = party.order_id;
    response.instrument = fill.instrument;
    response.side = side;
    response.price = fill.price;
    response.quantity = fill.quantity;
    response.leaves = party.remaining;
    response.execution_id = next_execution_id_;
    response.time = trade_time;
    send_private(party.session, response);
  }

  void reserve_notice(const ChangedOrder &order, std::uint64_t book_in_time) {
    const auto slot = free_notices_.back();
    free_notices_.pop_back();
    auto &identity = sessions_[order.session];
    auto &notice = notices_[slot];
    notice.owner = order.session;
    notice.record = {slot, private_sequence(identity), order.order_id, order.instrument,
                     identity.key.access, identity.key.partition, order.firm, book_in_time};
    notice.next = no_notice;
    notice.state = NoticeSlot::State::ready;
    if (identity.last_notice == no_notice)
      identity.first_notice = slot;
    else
      notices_[identity.last_notice].next = slot;
    identity.last_notice = slot;
    ready_notices_.push(slot);
  }

  void advance_notices() {
    for (auto batch : state_->workers->collect_notice_batches()) {
      auto flight = std::ranges::find(notice_batches_, batch, &NoticeBatch::id);
      if (flight == notice_batches_.end())
        throw std::logic_error("notice batch completion without a matching offer");
      for (auto slot : flight->slots) {
        auto &notice = notices_[slot];
        notice.state = NoticeSlot::State::copied;
        reap_notices(sessions_[notice.owner]);
      }
      flight->slots.clear();
      flight->id = 0;
      --outstanding_;
      if (state_->diag.enabled())
        event("notice_batch_released", Json::object().set("batch_id", batch));
    }
    if (ready_notices_.empty() || outstanding_ >= notice_batches_.size())
      return;
    const auto size = std::min(ready_notices_.size(), limits_.notice_batch);
    batch_.clear();
    for (std::size_t index = 0; index < size; ++index)
      batch_.push_back(notices_[ready_notices_.at(index)].record);
    if (next_batch_ == UINT64_MAX)
      throw std::overflow_error("notice batch sequence exhausted");
    auto flight = std::ranges::find(notice_batches_, std::uint64_t{0}, &NoticeBatch::id);
    if (!state_->workers->offer_notices(*this, next_batch_, batch_))
      return;
    flight->id = next_batch_++;
    ++outstanding_;
    for (std::size_t index = 0; index < size; ++index) {
      const auto slot = ready_notices_.front();
      ready_notices_.pop();
      notices_[slot].state = NoticeSlot::State::offered;
      flight->slots.push_back(slot);
    }
    update_peaks();
  }

  bool blocked(Command &command, std::string_view reason, std::size_t updates) {
    if (state_->diag.enabled() && command.reported_block != reason) {
      event("command_blocked", with_command(command).set("reason", reason)
                .set("required_updates", updates));
      command.reported_block = reason;
    }
    return false;
  }

  std::string_view validate_request(const Command &command) const {
    if (command.disconnect)
      return {};
    const auto &request = command.request;
    if (command.bad_sequence)
      return "invalid_client_sequence";
    if (std::ranges::find(sessions_[command.session].firms, request.firm) ==
        sessions_[command.session].firms.end())
      return "unauthorised_firm";
    if (request.client_order_id == optiq::null_client_order_id || request.emm != 1 ||
        request.has_optional_groups || request.execution_instruction != 0 ||
        request.dark_execution_instruction != 0 || request.time_in_force != 0 ||
        request.order_category != 1 ||
        (request.type == Template::NewOrder && request.order_type != 2))
      return "unsupported_order";
    return {};
  }

  bool apply(Command &command) {
    const auto &request = command.request;
    auto rejection = validate_request(command);
    const bool diagnostic = state_->diag.enabled();
    std::chrono::nanoseconds matcher_time{};
    if (rejection.empty()) {
      MatchKind kind = MatchKind::New;
      if (command.disconnect)
        kind = MatchKind::Disconnect;
      else if (request.type == Template::CancelRequest)
        kind = MatchKind::Cancel;
      else if (request.type == Template::CancelReplace)
        kind = MatchKind::Replace;
      const auto start = diagnostic ? Clock::now() : Clock::time_point{};
      matcher_.prepare({kind, command.session, request.firm, request.instrument, request.side,
                        request.price, request.quantity, request.order_id, request.order_type}, plan_);
      if (diagnostic)
        matcher_time = Clock::now() - start;
      rejection = plan_.rejection;
    } else {
      plan_.changes.clear();
      plan_.fills.clear();
    }

    if (rejection.empty() && plan_.needs_reservation &&
        live_reservations_ - plan_.reservations_released + notice_occupied() >= limits_.notices)
      rejection = "notice_capacity";

    std::ranges::fill(reply_demand_, 0);
    if (!command.disconnect)
      reply_demand_[command.session] = 1;
    if (rejection.empty()) {
      for (const auto &fill : plan_.fills) {
        ++reply_demand_[fill.maker.session];
        ++reply_demand_[fill.taker.session];
      }
      for (std::size_t i = 0; i < sessions_.size(); ++i) {
        if (sessions_[i].active != no_connection && reply_demand_[i] > limits_.reply_slots) {
          rejection = "reply_capacity";
          std::ranges::fill(reply_demand_, 0);
          reply_demand_[command.session] = 1;
          break;
        }
      }
    }
    for (std::size_t i = 0; i < sessions_.size(); ++i) {
      const auto demand = reply_demand_[i];
      if (!demand)
        continue;
      if (demand > connection_capacity(i))
        return blocked(command, "reply", 0);
      if (demand > UINT32_MAX - 2 - sessions_[i].outbound)
        throw std::overflow_error("private application sequence exhausted before applying command");
    }
    if (rejection.empty() && command.disconnect &&
        plan_.changes.size() > UINT32_MAX - 2 - sessions_[command.session].outbound) {
      throw std::overflow_error("disconnect notice sequence exhausted before removing orders");
    }
    if (rejection.empty() && plan_.fills.size() >= UINT32_MAX - next_execution_id_) {
      throw std::overflow_error("execution sequence exhausted before applying command");
    }

    auto book_in_time = optiq::null_order_id;
    if (rejection.empty()) {
      const auto available = std::min(state_->workers->market_available(),
                                      publication_cap_.value_or(limits_.publication));
      if (available < plan_.changes.size())
        return blocked(command, "publication", plan_.changes.size());
      book_in_time = command.disconnect
                         ? state_->transport.epoch_ns.load(std::memory_order_acquire)
                         : command.book_in_time;
      const auto start = diagnostic ? Clock::now() : Clock::time_point{};
      matcher_.commit(plan_);
      if (diagnostic)
        matcher_time += Clock::now() - start;
      live_reservations_ -= plan_.reservations_released;
      live_reservations_ += plan_.needs_reservation ? 1 : 0;
      if (command.disconnect) {
        for (const auto &change : plan_.changes)
          reserve_notice(change.order, book_in_time);
        auto &identity = sessions_[command.session];
        identity.disconnect_waiting = false;
      }
    }
    const auto applied = diagnostic ? sample() : Sample{};
    const auto trade_time = rejection.empty() && !plan_.fills.empty()
                                ? state_->transport.epoch_ns.load(std::memory_order_acquire) : 0;

    if (!command.disconnect)
      send_private(command.session,
                   response_for(request, plan_.result, rejection, !plan_.fills.empty(), book_in_time));
    const auto encoded = diagnostic ? sample() : Sample{};
    if (rejection.empty()) {
      for (const auto &fill : plan_.fills) {
        send_fill(fill.maker, fill, fill.maker_side, trade_time);
        send_fill(fill.taker, fill, fill.taker_side, trade_time);
        if (++next_execution_id_ == UINT32_MAX)
          throw std::overflow_error("execution sequence exhausted");
      }
      if (!plan_.changes.empty())
        state_->workers->publish(*this, plan_.changes, command.sequence);
    }
    update_peaks();
    state_->workers->record_command();
    if (diagnostic)
      report_completion(command, applied, encoded, rejection, matcher_time.count());
    return true;
  }

  static Json order_row(const BookOrder &row) {
    return Json::object().set("instrument", row.instrument).set("order_id", row.order_id)
        .set("side", static_cast<unsigned>(row.side)).set("price", row.price)
        .set("remaining", row.remaining).set("priority", row.priority);
  }

  void report_completion(const Command &command, const Sample &applied, const Sample &encoded,
                         std::string_view rejection, std::int64_t matcher_ns) {
    auto fills = Json::array();
    if (rejection.empty()) {
      for (const auto &fill : plan_.fills) {
        fills.push(Json::object().set("maker_order_id", fill.maker.order_id)
                       .set("taker_order_id", fill.taker.order_id).set("price", fill.price)
                       .set("quantity", fill.quantity));
      }
    }
    auto row = with_command(command);
    row.set("fills", std::move(fills))
        .set("market_update_count", rejection.empty() ? plan_.changes.size() : 0)
        .set("matcher_ns", matcher_ns);
    if (!rejection.empty())
      row.set("rejection_reason", rejection);
    if (!command.disconnect) {
      row.set("stages", Json::object().set("decode_started", command.decode_started.json())
                .set("frame_complete", command.decode_started.json())
                .set("book_applied", applied.json()).set("ack_encoded", encoded.json()));
    }
    event("command_complete", std::move(row));
  }

  Json snapshot() const {
    auto orders = Json::array();
    for (const auto &order : matcher_.snapshot())
      orders.push(order_row(order));
    auto queued = Json::array();
    for (std::size_t i = 0; i < commands_.size(); ++i)
      queued.push(commands_.at(i).sequence);
    std::vector<std::uint64_t> active;
    for (const auto &flight : notice_batches_) {
      if (flight.id)
        active.push_back(flight.id);
    }
    std::ranges::sort(active);
    auto handoff = Json::array();
    for (auto batch : active)
      handoff.push(batch);
    return Json::object().set("orders", std::move(orders))
        .set("queued_command_sequences", std::move(queued))
        .set("command_queue_entry_size", sizeof(Command)).set("peak_command_queue_depth", queue_peak_)
        .set("notice_slots_occupied", live_reservations_ + notice_occupied())
        .set("notice_slots_peak", notice_peak_).set("notice_handoff_outstanding", outstanding_)
        .set("notice_handoff_peak", handoff_peak_).set("notice_ready_waiting", ready_notices_.size())
        .set("notice_batches_in_flight", std::move(handoff));
  }

  void gate(const Json &request) {
    auto tag = request.get("name").string();
    auto value = request.get("value");
    if (tag == "command_processing") {
      process_commands_ = value.boolean();
    } else if (tag == "publication_credits") {
      publication_cap_ = value.is_null() ? std::nullopt : std::optional<std::size_t>(value.number());
    } else if (tag == "notice_copy") {
      state_->workers->set_notice_copy(value.boolean());
    } else {
      const auto access = request.get("logical_access_id").number();
      const auto partition = request.get("partition_id").number();
      if (access > UINT32_MAX || partition > UINT16_MAX)
        throw std::invalid_argument("invalid gate session");
      const auto identity = session_indices_.find({static_cast<std::uint32_t>(access),
                                                    static_cast<std::uint16_t>(partition)});
      if (identity == session_indices_.end())
        throw std::invalid_argument("unknown gate session");
      auto &send_gate = state_->transport.send_gates[identity->second];
      if (tag == "private_send")
        send_gate.allow.store(value.boolean(), std::memory_order_release);
      else if (tag == "private_send_bytes")
        send_gate.remaining.store(value.is_null() ? SIZE_MAX : value.number(),
                                  std::memory_order_release);
      else if (tag == "replay_send_through") {
        if (!value.is_null() && value.number() >= UINT32_MAX)
          throw std::invalid_argument("invalid replay gate");
        sessions_[identity->second].replay_through = value.is_null() ? std::nullopt :
            std::optional<std::uint32_t>(static_cast<std::uint32_t>(value.number()));
      } else
        throw std::invalid_argument("unknown gate");
    }
  }

  static void ok(Control &control, std::string result) {
    control.output = "{\"ok\":true,\"result\":" + result + "}\n";
  }

  bool serve_control(Control &control) {
    auto now = Clock::now();
    if (!control.output.empty()) {
      const auto data = std::string_view(control.output).substr(control.written);
      const auto size = ::send(control.fd.get(), data.data(), data.size(), MSG_NOSIGNAL);
      if (size <= 0)
        return size < 0 && app_io::retryable() && now <= control.deadline;
      control.written += static_cast<std::size_t>(size);
      return control.written < control.output.size();
    }

    if (!control.waiting) {
      std::array<char, 1024> chunk{};
      auto size = ::recv(control.fd.get(), chunk.data(), chunk.size(), 0);
      if (size <= 0)
        return size < 0 && app_io::retryable() && now < control.deadline;
      control.input.append(chunk.data(), static_cast<std::size_t>(size));
      if (control.input.size() > 8192)
        return false;
      auto end = control.input.find('\n');
      if (end == std::string::npos)
        return true;
      try {
        auto request = Json::parse(std::string_view(control.input).substr(0, end));
        auto operation = request.get("operation").string();
        if (operation == "wait") {
          auto timeout = bounded(request.get("timeout_ms"), 3000, "control wait interval");
          (void)request.get("event").string();
          (void)Json::object().includes(request.get("match"));
          control.deadline = now + std::chrono::milliseconds(timeout);
          control.waiting = std::move(request);
        } else if (operation == "snapshot")
          ok(control, snapshot().dump());
        else if (operation == "placement")
          ok(control, placement(*state_, true).dump());
        else if (operation == "statistics") {
          const auto stats = state_->workers->statistics();
          ok(control, Json::object().set("commands_completed", stats.commands_completed)
                          .set("market_updates_sent", stats.market_updates_sent)
                          .set("notices_copied", stats.notices_copied)
                          .set("publisher_callbacks", stats.publisher_callbacks).dump());
        }
        else if (operation == "gate") {
          gate(request);
          ok(control, "null");
        } else
          throw std::invalid_argument("unknown control operation");
      } catch (const std::exception &error) {
        control.output = Json::object().set("ok", false).set("error", error.what()).dump() + "\n";
      }
    }
    if (control.waiting) {
      const auto &request = *control.waiting;
      auto found = state_->diag.find(request.get("event").string(), request.get("match"));
      if (found)
        ok(control, std::move(*found));
      else if (now >= control.deadline)
        control.output = "{\"ok\":false,\"error\":\"event deadline expired\"}\n";
    }
    return true;
  }

  void manage_controls() {
    if (!control_.socket())
      return;
    if (auto fd = app_io::accept(control_.socket(), false); fd && controls_.size() < 16)
      controls_.emplace_back(std::move(fd));
    std::erase_if(controls_, [&](auto &control) { return !serve_control(control); });
  }

  void service_connections() {
    // BlueZone increments this counter after publishing each new open slot.
    // Existing slots stay in this list until the matcher retires them.
    const auto accepted = state_->transport.accepted_connections.load(std::memory_order_acquire);
    if (accepted != accepted_connections_) {
      accepted_connections_ = accepted;
      active_slots_.clear();
      for (std::size_t i = 0; i < connections_.size(); ++i) {
        const auto phase = connections_[i].transport.phase.load(std::memory_order_acquire);
        if (phase == Phase::open || phase == Phase::closed)
          active_slots_.push_back(i);
      }
    }

    std::erase_if(active_slots_, [&](std::size_t slot) {
      auto &connection = connections_[slot];
      auto &transport = connection.transport;
      if (transport.phase.load(std::memory_order_acquire) == Phase::closed) {
        disconnect(slot);
        transport.phase.store(Phase::retired, std::memory_order_release);
        return true;
      }
      if (!transport.close_requested.load(std::memory_order_relaxed)) {
        connection.open = true;
        attach(connection, slot);
        stage_replay(connection);
      }
      return false;
    });
    sequence_disconnects();

    if (active_slots_.empty())
      return;
    const auto next = std::ranges::lower_bound(active_slots_, input_start_);
    const auto size = active_slots_.size();
    const auto start = next == active_slots_.end() ? 0
                                                  : static_cast<std::size_t>(next - active_slots_.begin());
    std::size_t remaining = commands_per_turn;
    std::size_t count = 0;
    for (; count < size && commands_.available() && remaining; ++count) {
      const auto slot = active_slots_[(start + count) % size];
      remaining -= service_input(slot, std::min(remaining, order_transport::inbound_frames));
    }
    const auto advance = count > 0 && count < size ? count : 1;
    input_start_ = active_slots_[(start + advance) % size];
  }

public:
  explicit Gateway(const GatewayInit &init)
      : limits_(init.config.limits), state_(init.state),
        control_(init.options.control_path),
        matcher_(init.config.instruments, init.config.sessions.size(), limits_.book,
                 Actor::Allocator<std::byte>(getAllocator())),
        plan_(matcher_.make_plan()), commands_(limits_.queue),
        pending_disconnects_(init.config.sessions.size()),
        reply_demand_(init.config.sessions.size()), notices_(limits_.notices),
        ready_notices_(limits_.notices), notice_batches_(limits_.notice_handoff) {
    sessions_.reserve(init.config.sessions.size());
    for (const auto &rule : init.config.sessions) {
      session_indices_.emplace(rule.key, sessions_.size());
      sessions_.emplace_back(rule, limits_.ordinary_replay);
    }
    connections_.reserve(state_->transport.slots.size());
    active_slots_.reserve(state_->transport.slots.size());
    for (auto &slot : state_->transport.slots)
      connections_.emplace_back(slot);
    free_notices_.reserve(notices_.size());
    for (std::size_t i = notices_.size(); i != 0; --i)
      free_notices_.push_back(i - 1);
    batch_.reserve(limits_.notice_batch);
    for (auto &flight : notice_batches_)
      flight.slots.reserve(limits_.notice_batch);
    controls_.reserve(16);
    registerCallback(*this);
  }

  void onCallback() noexcept {
    try {
      if (state_->failed.load())
        return;
      if (state_->matcher_tid.load() < 0) {
        state_->matcher_cpu.store(::sched_getcpu());
        state_->matcher_tid.store(::gettid());
        registerCallback(*this);
        return;
      }
      if (state_->workers->market_failed()) {
        state_->fail("market subscriber lost or blocked; restart with an empty book");
        return;
      }
      manage_controls();
      advance_notices();
      service_connections();
      if (!commands_.available() && !full_reported_ && state_->diag.enabled()) {
        full_reported_ = true;
        event("intake_paused", Json::object().set("reason", "command_queue_full").set("queue_depth", commands_.size()));
      }
      for (std::size_t applied = 0;
           applied < commands_per_turn && process_commands_ && !commands_.empty() &&
           apply(commands_.front()); ++applied)
        commands_.pop();
      if (commands_.available())
        full_reported_ = false;
      const bool intake_paused = !commands_.available();
      if (state_->transport.intake_paused.load(std::memory_order_relaxed) != intake_paused)
        state_->transport.intake_paused.store(intake_paused, std::memory_order_release);
      registerCallback(*this);
    } catch (const std::exception &error) {
      state_->fail(error.what());
    } catch (...) {
      state_->fail("unknown gateway failure");
    }
  }
};

class AppExceptionHandler final : public tredzone::AsyncExceptionHandler {
  AppState &state_;
public:
  explicit AppExceptionHandler(AppState &state) : state_(state) {}
  void onEventException(Actor *, const std::type_info &, const char *, const Actor::Event &,
                        const char *reason) noexcept override {
    state_.fail(reason ? reason : "Simplx event exception");
  }
  void onUnreachableException(Actor &, const std::type_info &,
                             const Actor::ActorId::RouteIdComparable &,
                             const char *reason) noexcept override {
    state_.fail(reason ? reason : "Simplx unreachable exception");
  }
};

bool real_time(int id, unsigned expected_cpu) {
  cpu_set_t affinity;
  CPU_ZERO(&affinity);
  return ::sched_getscheduler(id) == SCHED_FIFO &&
         ::sched_getaffinity(id, sizeof(affinity), &affinity) == 0 &&
         CPU_COUNT(&affinity) == 1 && CPU_ISSET(expected_cpu, &affinity);
}

} // namespace

int run_app(const AppOptions &options, const volatile std::sig_atomic_t &stopping) {
  if (options.redzone && !options.control_path.empty())
    throw std::invalid_argument("--acceptance-control is incompatible with --redzone; use --placement-file");
  const auto config = load_config(options.config);
  const auto cores = choose_cores(options);
  if (options.redzone) {
    cpu_set_t housekeeping;
    CPU_ZERO(&housekeeping);
    CPU_SET(cores.blue, &housekeeping);
    if (::sched_setaffinity(0, sizeof(housekeeping), &housekeeping) != 0)
      throw std::runtime_error("RedZone run invalid: cannot pin supervisor thread to housekeeping");
  }
  auto state = std::make_shared<AppState>(config, !options.control_path.empty());
  WorkersConfig worker_options{
      options.bind_address, options.market_port, config.limits.publication,
      config.limits.notice_handoff, config.limits.notice_batch, config.limits.notices, {}};
  if (state->diag.enabled()) {
    worker_options.diagnostic = [state_ptr = state.get()](std::string_view event, std::string fields) {
      state_ptr->diag.record(event, Json::parse(fields));
    };
  }
  state->workers = std::make_shared<Workers>(std::move(worker_options));
  AppExceptionHandler errors(*state);
  Engine::CoreSet cpus;
  const auto matcher_cpu = static_cast<Actor::CoreId>(cores.matcher);
  const auto publisher_cpu = static_cast<Actor::CoreId>(cores.publisher);
  const auto blue_cpu = static_cast<Actor::CoreId>(cores.blue);
  cpus.set(matcher_cpu);
  cpus.set(publisher_cpu);
  cpus.set(blue_cpu);
  Engine::StartSequence start(cpus);
  start.setExceptionHandler(errors);
  if (options.redzone) {
    tredzone::ThreadRealTimeParam scheduling{};
    scheduling.sched_priority = 10;
    start.setRedZoneParam(scheduling);
    start.setRedZoneCore(matcher_cpu);
    start.setRedZoneCore(publisher_cpu);
  }
  state->workers->add_actors(start, publisher_cpu, blue_cpu);
  order_transport::add_actor(start, blue_cpu, options, state->transport, state->diag,
                             [state_ptr = state.get()](std::string_view error) {
                               state_ptr->fail(error);
                             });
  start.addActor<Gateway>(matcher_cpu, GatewayInit{options, config, state});
  // Simplx temporarily pins the engine constructor to each worker CPU for
  // allocation and actor initialization. Keep that work off the supervisor.
  int startup_tid{};
  auto engine = std::async(std::launch::async, [&] {
    startup_tid = ::gettid();
    return std::make_unique<Engine>(start);
  }).get();
  const auto until = Clock::now() + 3s;
  for (;;) {
    if (state->failed.load())
      throw std::runtime_error(state->error.data());
    if (state->matcher_tid.load() > 0 && state->transport.tid.load() > 0 &&
        std::ranges::all_of(state->workers->placement(), [](const auto &w) { return w.thread_id > 0; }))
      break;
    if (Clock::now() > until)
      throw std::runtime_error("Simplx workers failed to become ready");
    std::this_thread::sleep_for(1ms);
  }

  if (options.redzone) {
    const auto publisher = state->workers->placement().front();
    if (!real_time(state->matcher_tid.load(), cores.matcher) ||
        !real_time(publisher.thread_id, cores.publisher))
      throw std::runtime_error("RedZone run invalid: effective scheduling or affinity differs from configuration");
  }
  if (!options.placement_path.empty()) {
    const auto startup_task = std::filesystem::path("/proc/self/task") / std::to_string(startup_tid);
    // Linux can wake the joiner before removing the exiting thread from procfs.
    while (std::filesystem::exists(startup_task)) {
      if (Clock::now() > until)
        throw std::runtime_error("Simplx startup thread is still listed in /proc");
      std::this_thread::sleep_for(1ms);
    }
    std::ofstream file(options.placement_path);
    file << placement(*state, false).set("engine_startup_thread_id", startup_tid).dump() << '\n';
    file.close();
    if (!file)
      throw std::runtime_error("cannot write startup placement file: " + options.placement_path);
  }
  std::cout << "READY port=" << state->transport.port << " market_port=" << state->workers->market_port()
            << " matcher_cpu=" << cores.matcher << " publisher_cpu=" << cores.publisher
            << " blue_cpu=" << cores.blue << " scheduling=" << (options.redzone ? "redzone" : "ordinary")
            << std::endl;
  while (!stopping && !state->failed.load())
    std::this_thread::sleep_for(20ms);
  if (state->failed.load())
    throw std::runtime_error(state->error.data());
  return 0;
}

} // namespace compute_trade
