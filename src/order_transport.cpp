#include "order_transport.hpp"

#include "app_config.hpp"
#include "app_io.hpp"
#include "diagnostics.hpp"

#include <poll.h>
#include <sched.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cstring>
#include <span>

namespace compute_trade::order_transport {
namespace {

using namespace std::chrono_literals;
using app_io::Socket;
using Clock = std::chrono::steady_clock;
using tredzone::Actor;

constexpr std::size_t replies_per_turn = 8;
constexpr short peer_terminal_events = POLLRDHUP | POLLHUP | POLLERR;

std::uint64_t nanoseconds(auto since_epoch) {
  return static_cast<std::uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(since_epoch).count());
}

struct TcpConnection {
  Socket fd;
  std::array<std::byte, 4096> input{};
  std::size_t input_start{};
  std::size_t buffered{};
  std::size_t written{};
  Clock::time_point pending_since{};
  bool malformed_input{};
  bool write_reported{};
};

struct Init {
  const AppOptions &options;
  Transport &transport;
  Diagnostics &diagnostics;
  std::function<void(std::string_view)> fail;
};

class TcpWorker : public Actor, public Actor::Callback {
  Transport &transport_;
  Diagnostics &diag_;
  std::function<void(std::string_view)> fail_;
  Socket listener_;
  std::vector<TcpConnection> connections_;
  std::vector<pollfd> readiness_;
  std::vector<std::size_t> readiness_slots_;
  int tid_{-1};
  int cpu_{-1};

  Json with_session(std::size_t index) const {
    const auto &key = transport_.config.sessions[index].key;
    return Json::object().set("logical_access_id", key.access).set("partition_id", key.partition)
        .set("thread_id", tid_).set("cpu", cpu_)
        .set("iteration", getCorePerformanceCounters().getLoopTotalCount());
  }

  static void close(Slot &slot, TcpConnection &connection) {
    connection.fd.reset();
    slot.phase.store(Phase::closed, std::memory_order_release);
  }

  static void recycle(Slot &slot, TcpConnection &connection) {
    connection.fd.reset();
    slot.inbound.reset();
    slot.outbound.reset();
    slot.phase.store(Phase::free, std::memory_order_release);
  }

  void accept_connection(Clock::time_point now) {
    auto fd = app_io::accept(listener_);
    if (!fd)
      return;
    for (std::size_t index = 0; index < connections_.size(); ++index) {
      auto &slot = transport_.slots[index];
      if (slot.phase.load(std::memory_order_relaxed) != Phase::free)
        continue;
      auto &connection = connections_[index];
      connection.fd = std::move(fd);
      connection.input_start = 0;
      connection.buffered = 0;
      connection.written = 0;
      connection.pending_since = {};
      connection.malformed_input = false;
      connection.write_reported = false;
      slot.close_requested.store(false, std::memory_order_relaxed);
      slot.session.store(SIZE_MAX, std::memory_order_relaxed);
      slot.allow_intake.store(true, std::memory_order_relaxed);
      slot.opened_ns = nanoseconds(now.time_since_epoch());
      slot.phase.store(Phase::open, std::memory_order_release);
      transport_.accepted_connections.fetch_add(1, std::memory_order_release);
      return;
    }
  }

  void record_partial_input(const Slot &slot, const TcpConnection &connection) {
    if (!diag_.enabled() || !connection.buffered)
      return;
    const auto session = slot.session.load(std::memory_order_acquire);
    if (session != SIZE_MAX)
      diag_.record("input_buffered", with_session(session).set("buffered_bytes", connection.buffered));
  }

  void read_frames(Slot &slot, TcpConnection &connection, Clock::time_point now, bool readable) {
    bool read = false;
    std::size_t submitted = 0;
    while (submitted < inbound_frames && slot.inbound.available()) {
      if (connection.buffered >= optiq::header_size) {
        const auto input = std::span(connection.input)
                               .subspan(connection.input_start, connection.buffered);
        std::size_t length;
        try {
          // Decode the SOFH length to delimit the stream. The matcher decodes
          // the SBE message after taking ownership of its complete frame.
          length = optiq::frame_length(input);
        } catch (const std::invalid_argument &) {
          // Let the matcher reject this header after any earlier frames.
          optiq::Frame header;
          header.size = optiq::header_size;
          std::memcpy(header.bytes.data(), input.data(), header.size);
          slot.inbound.push(header);
          connection.malformed_input = true;
          return;
        }
        if (length <= connection.buffered) {
          optiq::Frame frame;
          std::memcpy(frame.bytes.data(), input.data(), length);
          frame.size = length;
          slot.inbound.push(frame);
          connection.buffered -= length;
          connection.input_start = connection.buffered ? connection.input_start + length : 0;
          ++submitted;
          continue;
        }
      }
      if (read || submitted) {
        if (read)
          record_partial_input(slot, connection);
        return;
      }
      // EOF observed by recv must follow frames already handed to the matcher.
      if (!slot.inbound.empty())
        return;
      const bool logon_timed_out =
          slot.session.load(std::memory_order_acquire) == SIZE_MAX &&
          nanoseconds(now.time_since_epoch()) - slot.opened_ns > nanoseconds(5s);
      if (!readable) {
        if (logon_timed_out)
          close(slot, connection);
        return;
      }
      auto end = connection.input_start + connection.buffered;
      if (end == connection.input.size()) {
        std::memmove(connection.input.data(), connection.input.data() + connection.input_start,
                     connection.buffered);
        connection.input_start = 0;
        end = connection.buffered;
      }
      const auto size = ::recv(connection.fd.get(), connection.input.data() + end,
                               connection.input.size() - end, 0);
      if (size <= 0) {
        if (size == 0 || !app_io::retryable() || logon_timed_out)
          close(slot, connection);
        return;
      }
      connection.buffered += static_cast<std::size_t>(size);
      read = true;
    }
  }

  bool write_frame(Slot &slot, TcpConnection &connection, Clock::time_point now, bool writable) {
    const auto *reply = slot.outbound.front();
    if (!reply)
      return false;
    if (connection.pending_since == Clock::time_point{})
      connection.pending_since = now;
    const auto frame = reply->frame.view().subspan(connection.written);
    const auto session = slot.session.load(std::memory_order_acquire);
    auto *gate = session == SIZE_MAX ? nullptr : &transport_.send_gates[session];
    auto budget = gate ? gate->remaining.load(std::memory_order_acquire) : SIZE_MAX;
    auto limit = std::min(frame.size(), budget);
    if (gate && !gate->allow.load(std::memory_order_acquire))
      limit = 0;
    if (!limit) {
      if (diag_.enabled() && !connection.write_reported && session != SIZE_MAX) {
        diag_.record("private_write_blocked", with_session(session).set("sequence", reply->sequence)
                         .set("bytes_written", connection.written));
        connection.write_reported = true;
      }
      return false;
    }
    if (!writable) {
      if (now - connection.pending_since > 30s)
        close(slot, connection);
      return false;
    }

    const auto sent = ::send(connection.fd.get(), frame.data(), limit, MSG_NOSIGNAL);
    if (sent <= 0) {
      if (sent == 0 || !app_io::retryable() || now - connection.pending_since > 30s)
        close(slot, connection);
      return false;
    }
    const auto count = static_cast<std::size_t>(sent);
    connection.pending_since = now;
    connection.written += count;
    if (gate && budget != SIZE_MAX)
      gate->remaining.compare_exchange_strong(budget, budget - count, std::memory_order_relaxed);
    if (connection.written != reply->frame.size)
      return false;

    if (diag_.enabled() && reply->sequence && session != SIZE_MAX)
      diag_.record("private_frame_written", with_session(session).set("sequence", reply->sequence));
    const bool end_connection = reply->end_connection;
    slot.outbound.pop();
    connection.written = 0;
    connection.write_reported = false;
    connection.pending_since = {};
    if (end_connection)
      close(slot, connection);
    return !end_connection;
  }

  bool intake_allowed(const Slot &slot) const {
    return slot.allow_intake.load(std::memory_order_acquire) &&
           !transport_.intake_paused.load(std::memory_order_acquire);
  }

  void poll_connections(Clock::time_point now) {
    readiness_.clear();
    readiness_slots_.clear();
    readiness_.push_back({listener_.get(), POLLIN, 0});
    for (std::size_t index = 0; index < connections_.size(); ++index) {
      auto &slot = transport_.slots[index];
      auto &connection = connections_[index];
      const auto phase = slot.phase.load(std::memory_order_acquire);
      if (phase == Phase::retired) {
        recycle(slot, connection);
        continue;
      }
      if (phase != Phase::open)
        continue;
      if (slot.close_requested.load(std::memory_order_acquire)) {
        close(slot, connection);
        continue;
      }
      short interests = POLLRDHUP;
      if (intake_allowed(slot) && !connection.malformed_input)
        interests |= POLLIN;
      if (!slot.outbound.empty())
        interests |= POLLOUT;
      readiness_.push_back({connection.fd.get(), interests, 0});
      readiness_slots_.push_back(index);
    }
    if (::poll(readiness_.data(), readiness_.size(), 0) < 0) {
      if (errno != EINTR)
        app_io::socket_error("poll order transport");
      return;
    }

    for (std::size_t entry = 0; entry < readiness_slots_.size(); ++entry) {
      const auto index = readiness_slots_[entry];
      auto &connection = connections_[index];
      auto &slot = transport_.slots[index];
      const auto events = readiness_[entry + 1].revents;
      const bool intake = intake_allowed(slot);
      if ((events & POLLNVAL) || (!intake && (events & peer_terminal_events))) {
        close(slot, connection);
        continue;
      }
      const bool writable = events & (POLLOUT | POLLHUP | POLLERR);
      for (std::size_t sent = 0; sent < replies_per_turn; ++sent) {
        if (!write_frame(slot, connection, now, writable))
          break;
      }
      if (intake && connection.fd && !connection.malformed_input) {
        const bool readable = events & (POLLIN | peer_terminal_events);
        if (readable || connection.buffered >= optiq::header_size ||
            slot.session.load(std::memory_order_acquire) == SIZE_MAX)
          read_frames(slot, connection, now, readable);
      }
    }
    if (readiness_.front().revents & POLLIN)
      accept_connection(now);
  }

public:
  explicit TcpWorker(const Init &init)
      : transport_(init.transport), diag_(init.diagnostics), fail_(init.fail),
        listener_(app_io::listener(init.options.bind_address, init.options.port, transport_.port)),
        connections_(transport_.slots.size()) {
    readiness_.reserve(connections_.size() + 1);
    readiness_slots_.reserve(connections_.size());
    registerCallback(*this);
  }

  void onCallback() noexcept {
    try {
      const auto now = Clock::now();
      transport_.monotonic_ns.store(nanoseconds(now.time_since_epoch()), std::memory_order_release);
      transport_.epoch_ns.store(nanoseconds(std::chrono::system_clock::now().time_since_epoch()),
                                std::memory_order_release);
      if (tid_ < 0) {
        tid_ = ::gettid();
        cpu_ = ::sched_getcpu();
        transport_.cpu.store(cpu_, std::memory_order_relaxed);
        transport_.tid.store(tid_, std::memory_order_release);
      }
      poll_connections(now);
      registerCallback(*this);
    } catch (const std::exception &error) {
      fail_(error.what());
    } catch (...) {
      fail_("unknown order transport failure");
    }
  }
};

} // namespace

Transport::Transport(const Config &configuration)
    : config(configuration), send_gates(config.sessions.size()) {
  const auto count = config.sessions.size() * 2 + 4;
  for (std::size_t index = 0; index < count; ++index)
    slots.emplace_back(config.limits.reply_slots);
}

void add_actor(tredzone::Engine::StartSequence &start, Actor::CoreId cpu, const AppOptions &options,
               Transport &transport, Diagnostics &diagnostics,
               std::function<void(std::string_view)> fail) {
  start.addActor<TcpWorker>(cpu, Init{options, transport, diagnostics, std::move(fail)});
}

} // namespace compute_trade::order_transport
