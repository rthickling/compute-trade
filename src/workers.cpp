#include "compute_trade/workers.hpp"
#include "compute_trade/matcher.hpp"
#include "market_wire.hpp"

#include <arpa/inet.h>
#include <netinet/tcp.h>
#include <sched.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <stdexcept>
#include <system_error>
#include <utility>
#include <vector>

namespace compute_trade {
namespace {

using tredzone::Actor;
using Clock = std::chrono::steady_clock;

class Socket {
public:
    explicit Socket(int fd = -1) noexcept : fd_(fd) {}
    ~Socket() { reset(); }
    Socket(const Socket &) = delete;
    Socket &operator=(const Socket &) = delete;

    int get() const noexcept { return fd_; }
    explicit operator bool() const noexcept { return fd_ >= 0; }
    void reset(int fd = -1) noexcept {
        if (fd_ >= 0)
            ::close(fd_);
        fd_ = fd;
    }

private:
    int fd_;
};

[[noreturn]] void socket_error(const char *what) {
    throw std::system_error(errno, std::generic_category(), what);
}

bool would_retry() noexcept {
    return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
}

// The matcher writes a slot while the publisher can read the adjacent slot.
struct alignas(64) MarketSlot {
    MarketUpdate update;
    std::uint64_t command_sequence;
};
static_assert(sizeof(MarketSlot) == 64);

MarketUpdate market_update(const BookChange &change) {
    const auto &order = change.order;
    MarketUpdate::Action action{};
    switch (change.kind) {
    case ChangeKind::Add: action = MarketUpdate::Action::add; break;
    case ChangeKind::Modify: action = MarketUpdate::Action::modify; break;
    case ChangeKind::Remove: action = MarketUpdate::Action::remove; break;
    }
    return {action, order.instrument, order.order_id, static_cast<std::uint8_t>(order.side),
            order.price, action == MarketUpdate::Action::remove ? 0 : order.remaining, order.priority};
}

struct MarketEvent : Actor::Event {
    std::uint64_t first;
    std::size_t count;
    std::uint64_t command_sequence;
    int sender_tid;
    int sender_cpu;

    MarketEvent(std::uint64_t begin, std::size_t size, std::uint64_t command,
                int thread, int cpu) noexcept
        : first(begin), count(size), command_sequence(command),
          sender_tid(thread), sender_cpu(cpu) {}
};

struct NoticeEvent : Actor::Event {
    std::size_t lane;
    explicit NoticeEvent(std::size_t index) noexcept : lane(index) {}
};

enum class BatchPhase { free, offered, copied };

struct NoticeLane {
    std::vector<NoticeRecord> records;
    std::uint64_t id{};
    std::size_t size{};
    std::atomic<BatchPhase> phase{BatchPhase::free};
};

struct RetainedNotice {
    NoticeRecord record{};
    std::atomic<bool> available{false};
};

struct WorkerThread {
    std::atomic<int> tid{-1};
    std::atomic<int> cpu{-1};

    void identify() noexcept {
        cpu.store(::sched_getcpu(), std::memory_order_relaxed);
        tid.store(::gettid(), std::memory_order_release);
    }
    WorkerLocation location(std::string_view role) const noexcept {
        const auto thread = tid.load(std::memory_order_acquire);
        return {role, cpu.load(std::memory_order_relaxed), thread};
    }
};

} // namespace

struct Workers::Impl {
    class Publisher;
    class Blue;
    class MarketTransport;

    explicit Impl(WorkersConfig value)
        : config(std::move(value)), market(config.market_capacity),
          encoded_market(config.market_capacity),
          lanes(config.notice_handoff_slots), retained(config.notice_capacity) {
        if (market.empty() || lanes.empty() || retained.empty() || !config.notice_batch_size)
            throw std::invalid_argument("all worker capacities must be positive");
        for (auto &lane : lanes)
            lane.records.resize(config.notice_batch_size);
        collected.reserve(lanes.size());
    }

    void log(std::string_view event, std::string fields) const {
        if (config.diagnostic)
            config.diagnostic(event, std::move(fields));
    }

    WorkersConfig config;
    std::vector<MarketSlot> market;
    std::vector<market_wire::UpdateFrame> encoded_market;
    std::vector<NoticeLane> lanes;
    std::vector<RetainedNotice> retained;
    std::vector<std::uint64_t> collected;
    Actor::ActorId publisher_id;
    Actor::ActorId blue_id;
    WorkerThread publisher_thread;
    WorkerThread blue_thread;
    std::atomic<std::uint16_t> port{};
    std::atomic<bool> market_ready{false};
    std::atomic<bool> market_broken{false};
    std::atomic<bool> notice_copy{true};
    // Market transport returns credits after the full frame goes out. The matcher advances production, and
    // BlueZone reads the command count independently of publication.
    alignas(64) std::atomic<std::uint64_t> market_read{};
    alignas(64) std::atomic<std::uint64_t> market_published{};
    alignas(64) std::atomic<std::uint64_t> market_encoded{};
    alignas(64) std::atomic<std::uint64_t> completed{};
    alignas(64) std::atomic<std::uint64_t> sampled_completed{};
    std::atomic<std::uint64_t> copied_notices{};
    std::atomic<std::uint64_t> publisher_callbacks{};
};

class Workers::Impl::Publisher : public Actor, public Actor::Callback {
public:
    explicit Publisher(Impl *state) : state_(*state) {
        state_.publisher_id = getActorId();
        registerEventHandler<MarketEvent>(*this);
        registerCallback(*this);
    }

    void onEvent(const MarketEvent &event) {
        if (event.first != event_tail_)
            throw std::logic_error("market Event out of sequence");
        event_tail_ += event.count;
        registerCallback(*this);
        if (state_.config.diagnostic) {
            const auto self = state_.publisher_thread.location("publisher");
            state_.log("market_event_received",
                       "{\"command_sequence\":" + std::to_string(event.command_sequence) +
                       ",\"sender_thread_id\":" + std::to_string(event.sender_tid) +
                       ",\"sender_cpu\":" + std::to_string(event.sender_cpu) +
                       ",\"receiver_thread_id\":" + std::to_string(self.thread_id) +
                       ",\"receiver_cpu\":" + std::to_string(self.cpu) + "}");
        }
    }

    void onCallback() noexcept {
        if (state_.config.diagnostic)
            state_.publisher_callbacks.fetch_add(1, std::memory_order_relaxed);
        if (!identified_) {
            state_.publisher_thread.identify();
            identified_ = true;
        }
        if (encoded_ == event_tail_)
            return;

        const auto published = state_.market_published.load(std::memory_order_acquire);
        const auto limit = std::min(event_tail_, published);
        const auto count = std::min<std::uint64_t>(limit - encoded_, 32);
        for (std::uint64_t end = encoded_ + count; encoded_ != end; ++encoded_) {
            const auto slot = encoded_ % state_.market.size();
            const auto &record = state_.market[slot];
            market_wire::encode(state_.encoded_market[slot], record.update,
                                record.command_sequence, encoded_ + 1);
        }
        if (count)
            state_.market_encoded.store(encoded_, std::memory_order_release);
        // Continue only while a received Event has work left. A batch may
        // span several callbacks or still be awaiting the matcher's frontier.
        if (encoded_ != event_tail_)
            registerCallback(*this);
    }

private:
    Impl &state_;
    std::uint64_t encoded_{};
    std::uint64_t event_tail_{};
    bool identified_{};
};

class Workers::Impl::MarketTransport : public Actor, public Actor::Callback {
public:
    explicit MarketTransport(Impl *state) : state_(*state) {
        listener_.reset(::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
        if (!listener_)
            socket_error("market socket");
        const int reuse = 1;
        if (::setsockopt(listener_.get(), SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse)) < 0)
            socket_error("market SO_REUSEADDR");
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_port = htons(state_.config.market_port);
        if (::inet_pton(AF_INET, state_.config.bind_address.c_str(), &address.sin_addr) != 1)
            throw std::invalid_argument("invalid market bind address");
        if (::bind(listener_.get(), reinterpret_cast<sockaddr *>(&address), sizeof(address)) < 0)
            socket_error("market bind");
        if (::listen(listener_.get(), 4) < 0)
            socket_error("market listen");
        socklen_t length = sizeof(address);
        if (::getsockname(listener_.get(), reinterpret_cast<sockaddr *>(&address), &length) < 0)
            socket_error("market getsockname");
        state_.port.store(ntohs(address.sin_port), std::memory_order_release);

        registerCallback(*this);
    }

    void onCallback() noexcept {
        if (!state_.market_broken.load(std::memory_order_relaxed)) {
            if (!ready_)
                subscribe();
            if (ready_)
                drain_updates();
        }
        registerCallback(*this);
    }

private:
    static constexpr std::size_t max_send_frames = 32;

    void reset_handshake() noexcept {
        client_.reset();
        input_size_ = 0;
        sent_ = 0;
        subscribed_ = false;
    }

    void subscribe() noexcept {
        if (!client_) {
            client_.reset(::accept4(listener_.get(), nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC));
            if (!client_)
                return;
            const int yes = 1;
            if (::setsockopt(client_.get(), IPPROTO_TCP, TCP_NODELAY, &yes, sizeof(yes)) < 0) {
                reset_handshake();
                return;
            }
            handshake_started_ = Clock::now();
        }
        if (Clock::now() - handshake_started_ > std::chrono::seconds(5)) {
            reset_handshake();
            return;
        }
        if (!subscribed_) {
            const auto got = ::recv(client_.get(), input_.data() + input_size_,
                                    input_.size() - input_size_, 0);
            if (got < 0 && would_retry())
                return;
            if (got <= 0) {
                reset_handshake();
                return;
            }
            input_size_ += static_cast<std::size_t>(got);
            if (input_size_ != input_.size())
                return;
            if (input_ != market_wire::subscribe) {
                reset_handshake();
                return;
            }
            subscribed_ = true;
        }
        if (!send_ready()) {
            if (closed_)
                reset_handshake();
            return;
        }
        ready_ = true;
        state_.market_ready.store(true, std::memory_order_release);
    }

    bool send_ready() noexcept {
        closed_ = false;
        const auto frame = std::span(market_wire::ready).subspan(sent_);
        const auto count = ::send(client_.get(), frame.data(), frame.size(), MSG_NOSIGNAL);
        if (count > 0) {
            sent_ += static_cast<std::size_t>(count);
            if (static_cast<std::size_t>(count) == frame.size()) {
                sent_ = 0;
                return true;
            }
            return false;
        }
        closed_ = count == 0 || !would_retry();
        return false;
    }

    void drain_updates() noexcept {
        const auto visible = state_.market_encoded.load(std::memory_order_acquire);
        if (read_ == visible)
            return;
        const auto batch = std::min<std::uint64_t>(visible - read_, max_send_frames);
        std::array<iovec, max_send_frames> buffers;
        for (std::size_t i = 0; i < batch; ++i) {
            auto &frame = state_.encoded_market[(read_ + i) % state_.encoded_market.size()].bytes;
            const auto offset = i == 0 ? sent_ : 0;
            buffers[i] = {frame.data() + offset, frame.size() - offset};
        }
        msghdr message{};
        message.msg_iov = buffers.data();
        message.msg_iovlen = batch;
        const auto count = ::sendmsg(client_.get(), &message, MSG_NOSIGNAL);
        if (count <= 0) {
            const bool closed = count == 0 || !would_retry();
            const auto now = Clock::now();
            if (closed || (blocked_since_ && now - *blocked_since_ > std::chrono::seconds(30))) {
                client_.reset();
                state_.market_broken.store(true, std::memory_order_release);
            } else if (!blocked_since_) {
                blocked_since_ = now;
            }
            return;
        }

        blocked_since_.reset();
        const auto bytes = static_cast<std::size_t>(count) + sent_;
        constexpr auto frame_size = market_wire::UpdateFrame{}.bytes.size();
        const auto completed = bytes / frame_size;
        sent_ = bytes % frame_size;
        if (completed) {
            read_ += completed;
            state_.market_read.store(read_, std::memory_order_release);
            try {
                if (state_.config.diagnostic)
                    state_.log("market_frames_sent", "{\"sequence\":" + std::to_string(read_) +
                               ",\"thread_id\":" + std::to_string(::gettid()) +
                               ",\"cpu\":" + std::to_string(::sched_getcpu()) + "}");
            } catch (...) {
                // Test diagnostics do not affect delivery or credit return.
            }
        }
    }

    Impl &state_;
    Socket listener_;
    Socket client_;
    std::array<std::byte, market_wire::subscribe.size()> input_{};
    std::size_t input_size_{};
    std::size_t sent_{};
    std::uint64_t read_{};
    Clock::time_point handshake_started_{};
    std::optional<Clock::time_point> blocked_since_;
    bool ready_{};
    bool subscribed_{};
    bool closed_{};
};

class Workers::Impl::Blue : public Actor, public Actor::Callback {
public:
    explicit Blue(Impl *state) : state_(*state), pending_(state_.lanes.size()) {
        state_.blue_id = getActorId();
        registerEventHandler<NoticeEvent>(*this);
        registerCallback(*this);
    }

    void onEvent(const NoticeEvent &event) {
        // Every pending Event owns a distinct lane until its copy completes.
        if (pending_count_ == pending_.size())
            throw std::logic_error("notice Event queue overflow");
        if (event.lane >= state_.lanes.size() ||
            state_.lanes[event.lane].phase.load(std::memory_order_acquire) != BatchPhase::offered)
            throw std::logic_error("received a notice batch without a reservation");
        pending_[(head_ + pending_count_) % pending_.size()] = event.lane;
        ++pending_count_;
    }

    void onCallback() noexcept {
        if (!identified_) {
            state_.blue_thread.identify();
            identified_ = true;
        }
        if (state_.notice_copy.load(std::memory_order_acquire))
            for (unsigned work = 0; work < 8 && pending_count_; ++work)
                copy_batch();
        // Aggregate command counts periodically away from the matching worker.
        if (++ticks_ == 4096) {
            ticks_ = 0;
            state_.sampled_completed.store(state_.completed.load(std::memory_order_relaxed),
                                           std::memory_order_relaxed);
        }
        registerCallback(*this);
    }

private:
    void copy_batch() noexcept {
        auto &lane = state_.lanes[pending_[head_]];
        for (std::size_t item = 0; item < lane.size; ++item) {
            auto &target = state_.retained[lane.records[item].slot];
            target.record = lane.records[item];
            target.available.store(true, std::memory_order_release);
        }
        state_.copied_notices.fetch_add(lane.size, std::memory_order_relaxed);
        head_ = (head_ + 1) % pending_.size();
        --pending_count_;
        try {
            if (state_.config.diagnostic)
                state_.log("notice_batch_copied", "{\"batch_id\":" + std::to_string(lane.id) + "}");
        } catch (...) {
            // Complete the handoff even if optional diagnostics run out of memory.
        }
        lane.phase.store(BatchPhase::copied, std::memory_order_release);
    }

    Impl &state_;
    std::vector<std::size_t> pending_;
    std::size_t head_{};
    std::size_t pending_count_{};
    unsigned ticks_{};
    bool identified_{};
};

Workers::Workers(WorkersConfig config) : impl_(std::make_unique<Impl>(std::move(config))) {}
Workers::~Workers() = default;

void Workers::add_actors(tredzone::Engine::StartSequence &sequence,
                         Actor::CoreId publisher_cpu, Actor::CoreId blue_cpu) {
    sequence.addActor<Impl::Publisher>(publisher_cpu, impl_.get());
    sequence.addActor<Impl::Blue>(blue_cpu, impl_.get());
    sequence.addActor<Impl::MarketTransport>(blue_cpu, impl_.get());
}

std::uint16_t Workers::market_port() const noexcept {
    return impl_->port.load(std::memory_order_acquire);
}

bool Workers::market_failed() const noexcept {
    return impl_->market_broken.load(std::memory_order_acquire);
}

std::array<WorkerLocation, 2> Workers::placement() const noexcept {
    return {impl_->publisher_thread.location("publisher"),
            impl_->blue_thread.location("bluezone")};
}

WorkerStatistics Workers::statistics() const noexcept {
    return {impl_->sampled_completed.load(std::memory_order_relaxed),
            impl_->market_read.load(std::memory_order_relaxed),
            impl_->copied_notices.load(std::memory_order_relaxed),
            impl_->publisher_callbacks.load(std::memory_order_relaxed)};
}

std::size_t Workers::market_available() const noexcept {
    if (!impl_->market_ready.load(std::memory_order_acquire) ||
        impl_->market_broken.load(std::memory_order_acquire))
        return 0;
    const auto written = impl_->market_published.load(std::memory_order_relaxed);
    const auto read = impl_->market_read.load(std::memory_order_acquire);
    return impl_->market.size() - static_cast<std::size_t>(written - read);
}

void Workers::publish(Actor &sender, std::span<const BookChange> changes,
                      std::uint64_t command_sequence) {
    if (changes.empty())
        return;
    const auto consumed = impl_->market_read.load(std::memory_order_acquire);
    const auto first = impl_->market_published.load(std::memory_order_relaxed);
    const auto room = impl_->market.size() - static_cast<std::size_t>(first - consumed);
    if (changes.size() > room)
        throw std::logic_error("market publication was not reserved");
    for (std::size_t index = 0; index < changes.size(); ++index)
        impl_->market[(first + index) % impl_->market.size()] =
            {market_update(changes[index]), command_sequence};
    const auto diagnostic = static_cast<bool>(impl_->config.diagnostic);
    Actor::Event::Pipe(sender, impl_->publisher_id).push<MarketEvent>(
        first, changes.size(), command_sequence, diagnostic ? ::gettid() : -1,
        diagnostic ? ::sched_getcpu() : -1);
    impl_->market_published.store(first + changes.size(), std::memory_order_release);
}

bool Workers::offer_notices(Actor &sender, std::uint64_t batch_id,
                            std::span<const NoticeRecord> records) {
    if (records.empty() || records.size() > impl_->config.notice_batch_size)
        throw std::invalid_argument("invalid notice batch size");
    for (std::size_t index = 0; index < impl_->lanes.size(); ++index) {
        auto &lane = impl_->lanes[index];
        if (lane.phase.load(std::memory_order_relaxed) != BatchPhase::free)
            continue;
        for (const auto &record : records)
            if (record.slot >= impl_->retained.size() ||
                impl_->retained[record.slot].available.load(std::memory_order_acquire))
                throw std::logic_error("notice storage slot not free");
        std::copy(records.begin(), records.end(), lane.records.begin());
        lane.id = batch_id;
        lane.size = records.size();
        lane.phase.store(BatchPhase::offered, std::memory_order_release);
        try {
            Actor::Event::Pipe(sender, impl_->blue_id).push<NoticeEvent>(index);
        } catch (...) {
            // Simplx allocates and constructs before its noexcept chain append.
            lane.phase.store(BatchPhase::free, std::memory_order_relaxed);
            throw;
        }
        return true;
    }
    return false;
}

std::span<const std::uint64_t> Workers::collect_notice_batches() noexcept {
    impl_->collected.clear();
    for (auto &lane : impl_->lanes) {
        if (lane.phase.load(std::memory_order_acquire) == BatchPhase::copied) {
            impl_->collected.push_back(lane.id);
            lane.phase.store(BatchPhase::free, std::memory_order_relaxed);
        }
    }
    return impl_->collected;
}

std::optional<NoticeRecord> Workers::retained_notice(std::size_t slot) const noexcept {
    const auto &notice = impl_->retained[slot];
    if (!notice.available.load(std::memory_order_acquire))
        return std::nullopt;
    return notice.record;
}

void Workers::release_notice(std::size_t slot) noexcept {
    impl_->retained[slot].available.store(false, std::memory_order_release);
}

void Workers::set_notice_copy(bool enabled) noexcept {
    impl_->notice_copy.store(enabled, std::memory_order_release);
}

void Workers::record_command() noexcept {
    // The matcher is the sole writer, so it can increment without a locked RMW.
    const auto completed = impl_->completed.load(std::memory_order_relaxed);
    impl_->completed.store(completed + 1, std::memory_order_relaxed);
}

} // namespace compute_trade
