#include "compute_trade/exchange.hpp"
#include "compute_trade/order_book.hpp"
#include "compute_trade/sbe.hpp"

#include <simplx.h>

#include <arpa/inet.h>
#include <netinet/tcp.h>
#include <sched.h>
#include <sys/socket.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <cerrno>
#include <cstring>
#include <limits>
#include <list>
#include <map>
#include <mutex>
#include <stdexcept>
#include <system_error>
#include <unordered_set>
#include <utility>

namespace compute_trade {
namespace {

using tredzone::Actor;
using tredzone::Engine;

class Socket {
public:
    explicit Socket(int fd = -1) noexcept : fd_(fd) {}
    ~Socket() { if (fd_ >= 0) ::close(fd_); }
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;
    Socket(Socket&& other) noexcept : fd_(std::exchange(other.fd_, -1)) {}
    Socket& operator=(Socket&& other) noexcept {
        if (this != &other) {
            if (fd_ >= 0) ::close(fd_);
            fd_ = std::exchange(other.fd_, -1);
        }
        return *this;
    }
    int get() const noexcept { return fd_; }
private:
    int fd_;
};

[[noreturn]] void socket_error(const char* operation) {
    throw std::system_error(errno, std::generic_category(), operation);
}

struct SharedState {
    std::atomic<bool> failed{false};
    std::atomic<int> oeg_cpu{-1};
    std::atomic<int> matching_cpu{-1};
    std::atomic<std::uint16_t> port{0};
    mutable std::mutex mutex;
    std::string error;

    void fail(const char* message) noexcept {
        try {
            std::lock_guard lock(mutex);
            if (!failed.load()) error = message;
            failed.store(true);
        } catch (...) {
            failed.store(true);
        }
    }
};

struct ActorConfig {
    ExchangeConfig exchange;
    std::shared_ptr<SharedState> shared;
};

struct Request : Actor::Event {
    NewOrder order;
    std::uint64_t session;
    RejectReason rejection;
    Request(const NewOrder& value, std::uint64_t session_id, RejectReason reason)
        : order(value), session(session_id), rejection(reason) {}
};

struct Response : Actor::Event {
    Acknowledgement acknowledgement;
    std::uint64_t session;
    Response(const Acknowledgement& value, std::uint64_t session_id)
        : acknowledgement(value), session(session_id) {}
};

struct MatchingService : tredzone::Service {};

Acknowledgement rejected(const NewOrder& order, RejectReason reason) {
    return {order.client_order_id, 0, order.instrument_id, Status::Rejected, reason, 0, 0};
}

class MatchingEngineActor : public Actor {
    using Book = OrderBook<Allocator<std::byte>>;
    using BookEntry = std::pair<const std::uint32_t, Book>;
public:
    explicit MatchingEngineActor(const ActorConfig& config)
        : shared_(config.shared), books_(std::less<std::uint32_t>{}, getAllocator()) {
        for (const auto instrument : config.exchange.instruments) {
            books_.try_emplace(instrument, Allocator<std::byte>(getAllocator()));
        }
        shared_->matching_cpu.store(::sched_getcpu());
        registerEventHandler<Request>(*this);
    }

    void onEvent(const Request& event) {
        if (shared_->failed.load()) return;
        auto ack = rejected(event.order, event.rejection);
        if (event.rejection == RejectReason::None) {
            auto book = books_.find(event.order.instrument_id);
            if (book == books_.end()) {
                ack.reason = RejectReason::UnknownInstrument;
            } else {
                if (next_order_id_ == std::numeric_limits<std::uint64_t>::max()) {
                    throw std::overflow_error("exchange order ID space exhausted");
                }
                ack = book->second.submit(event.order, next_order_id_);
                if (ack.status == Status::Accepted) ++next_order_id_;
            }
        }
        // Same-core delivery still uses Simplx's Event allocator and event lifetime rules.
        Event::Pipe(*this, event.getSourceActorId()).push<Response>(ack, event.session);
    }

private:
    std::shared_ptr<SharedState> shared_;
    std::map<std::uint32_t, Book, std::less<std::uint32_t>, Allocator<BookEntry>> books_;
    std::uint64_t next_order_id_{1};
};

// A small nonblocking TCP adapter in the OEG actor. Each callback does bounded work,
// so a busy socket cannot prevent matching events from being dispatched on this loop.
class OegActor : public Actor, public Actor::Callback {
    static constexpr std::size_t max_sessions = 16;
    static constexpr std::size_t window = 32;
    static constexpr std::size_t receive_capacity = 2048;

    struct Session {
        Socket socket;
        std::uint64_t id;
        std::array<std::byte, receive_capacity> receive{};
        std::size_t received{};
        std::array<sbe::AcknowledgementFrame, window> transmit{};
        std::size_t head{};
        std::size_t queued{};
        std::size_t sent{};
        // Includes matching requests and acknowledgements waiting for transmission.
        std::size_t outstanding{};
        bool read_closed{};
        std::unordered_set<std::uint64_t, std::hash<std::uint64_t>, std::equal_to<std::uint64_t>,
                           Allocator<std::uint64_t>> client_ids;

        Session(Socket fd, std::uint64_t session_id, AllocatorBase allocator)
            : socket(std::move(fd)), id(session_id),
              client_ids(0, std::hash<std::uint64_t>{}, std::equal_to<std::uint64_t>{}, allocator) {}
    };

public:
    explicit OegActor(const ActorConfig& config)
        : shared_(config.shared), sessions_(getAllocator()),
          matching_(*this, getEngine().getServiceIndex().getServiceActorId<MatchingService>()) {
        listener_ = Socket(::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0));
        if (listener_.get() < 0) socket_error("socket");
        const int reuse = 1;
        if (::setsockopt(listener_.get(), SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse)) < 0)
            socket_error("SO_REUSEADDR");
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_port = htons(config.exchange.port);
        if (::inet_pton(AF_INET, config.exchange.bind_address.c_str(), &address.sin_addr) != 1)
            throw std::invalid_argument("bind address must be an IPv4 address");
        if (::bind(listener_.get(), reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0)
            socket_error("bind");
        if (::listen(listener_.get(), static_cast<int>(max_sessions)) < 0) socket_error("listen");
        socklen_t size = sizeof(address);
        if (::getsockname(listener_.get(), reinterpret_cast<sockaddr*>(&address), &size) < 0)
            socket_error("getsockname");
        shared_->port.store(ntohs(address.sin_port));
        shared_->oeg_cpu.store(::sched_getcpu());
        registerEventHandler<Response>(*this);
        registerUndeliveredEventHandler<Request>(*this);
        registerCallback(*this);
    }

    void onCallback() noexcept {
        if (shared_->failed.load()) return;
        try {
            accept_one();
            for (auto session = sessions_.begin(); session != sessions_.end();) {
                if (!service(*session)) session = sessions_.erase(session);
                else ++session;
            }
            registerCallback(*this);
        } catch (const std::exception& error) {
            shared_->fail(error.what());
        } catch (...) {
            shared_->fail("unexpected OEG callback failure");
        }
    }

    void onEvent(const Response& event) {
        for (auto& session : sessions_) {
            if (session.id != event.session) continue;
            if (session.queued == window) throw std::logic_error("OEG response window overflow");
            session.transmit[(session.head + session.queued) % window] =
                sbe::encode(event.acknowledgement);
            ++session.queued;
            return;
        }
        // A disconnected session cannot receive its result. Session IDs are never reused.
    }

    void onUndeliveredEvent(const Request&) {
        shared_->fail("matching actor did not accept an order event");
    }

private:
    void accept_one() {
        Socket incoming(::accept4(listener_.get(), nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC));
        if (incoming.get() < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR || errno == ECONNABORTED) return;
            socket_error("accept");
        }
        if (sessions_.size() == max_sessions) return; // Closing excess clients bounds transport memory.
        const int no_delay = 1;
        if (::setsockopt(incoming.get(), IPPROTO_TCP, TCP_NODELAY, &no_delay, sizeof(no_delay)) < 0)
            socket_error("TCP_NODELAY");
        if (next_session_ == std::numeric_limits<std::uint64_t>::max())
            throw std::overflow_error("session ID space exhausted");
        sessions_.emplace_back(std::move(incoming), next_session_++, getAllocator());
    }

    bool flush(Session& session) {
        // At most one syscall per session per callback, including partial writes.
        if (session.queued == 0) return true;
        const auto& frame = session.transmit[session.head];
        const auto size = ::send(session.socket.get(), frame.data() + session.sent,
                                 frame.size() - session.sent, MSG_NOSIGNAL);
        if (size < 0) return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR;
        if (size == 0) return false;
        session.sent += static_cast<std::size_t>(size);
        if (session.sent == frame.size()) {
            session.head = (session.head + 1) % window;
            --session.queued;
            --session.outstanding;
            session.sent = 0;
        }
        return true;
    }

    bool parse(Session& session) {
        std::size_t consumed = 0;
        while (session.outstanding < window && session.received - consumed >= sbe::sofh_size) {
            const auto available = std::span(session.receive).subspan(consumed, session.received - consumed);
            std::size_t frame_size;
            try { frame_size = sbe::frame_length(available); }
            catch (const std::invalid_argument&) { return false; } // Invalid framing: cannot resynchronise.
            if (available.size() < frame_size) break;
            NewOrder order{};
            RejectReason rejection = RejectReason::None;
            try { order = sbe::decode_new_order(available.first(frame_size)); }
            catch (const std::invalid_argument&) { rejection = RejectReason::MalformedMessage; }
            if (rejection == RejectReason::None && order.client_order_id != 0 &&
                !session.client_ids.insert(order.client_order_id).second)
                rejection = RejectReason::DuplicateClientOrderId;
            // Route rejections through the same pipe to preserve per-session response order.
            matching_.push<Request>(order, session.id, rejection);
            ++session.outstanding;
            consumed += frame_size;
        }
        if (consumed != 0) {
            session.received -= consumed;
            std::memmove(session.receive.data(), session.receive.data() + consumed, session.received);
        }
        return true;
    }

    bool service(Session& session) {
        if (!flush(session) || !parse(session)) return false;
        if (!session.read_closed && session.outstanding < window && session.received < receive_capacity) {
            const auto size = ::recv(session.socket.get(), session.receive.data() + session.received,
                                     receive_capacity - session.received, 0);
            if (size > 0) session.received += static_cast<std::size_t>(size);
            else if (size == 0) session.read_closed = true;
            else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) return false;
            if (!parse(session)) return false;
        }
        // FIN closes only the read side: finish complete requests and their responses first.
        if (session.read_closed && session.outstanding == 0) return false;
        return true;
    }

    std::shared_ptr<SharedState> shared_;
    Socket listener_;
    std::list<Session, Allocator<Session>> sessions_;
    Event::Pipe matching_;
    std::uint64_t next_session_{1};
};

class ExceptionHandler final : public tredzone::AsyncExceptionHandler {
public:
    explicit ExceptionHandler(std::shared_ptr<SharedState> shared) : shared_(std::move(shared)) {}
    void onEventException(Actor*, const std::type_info&, const char*, const Actor::Event&,
                          const char* message) noexcept override {
        shared_->fail(message ? message : "unknown actor event exception");
    }
    void onUnreachableException(Actor&, const std::type_info&, const Actor::ActorId::RouteIdComparable&,
                                const char* message) noexcept override {
        shared_->fail(message ? message : "unreachable actor");
    }
private:
    std::shared_ptr<SharedState> shared_;
};

} // namespace

void validate_core(unsigned core) {
    cpu_set_t allowed;
    CPU_ZERO(&allowed);
    if (::sched_getaffinity(0, sizeof(allowed), &allowed) < 0) socket_error("sched_getaffinity");
    if (core > std::numeric_limits<Actor::CoreId>::max() || core >= CPU_SETSIZE ||
        !CPU_ISSET(core, &allowed))
        throw std::invalid_argument("core must be an allowed Linux CPU ID between 0 and 255");
}

unsigned first_allowed_core() {
    cpu_set_t allowed;
    CPU_ZERO(&allowed);
    if (::sched_getaffinity(0, sizeof(allowed), &allowed) < 0) socket_error("sched_getaffinity");
    for (unsigned cpu = 0; cpu <= std::numeric_limits<Actor::CoreId>::max(); ++cpu)
        if (CPU_ISSET(cpu, &allowed)) return cpu;
    throw std::runtime_error("no allowed CPU representable by Simplx's 8-bit CoreId");
}

struct Exchange::Impl {
    std::shared_ptr<SharedState> shared = std::make_shared<SharedState>();
    ExceptionHandler exceptions{shared}; // Must outlive the engine.
    unsigned core;
    std::unique_ptr<Engine> engine;

    explicit Impl(ExchangeConfig config) : core(config.core) {
        validate_core(core);
        if (config.instruments.empty()) throw std::invalid_argument("at least one instrument is required");
        std::unordered_set<std::uint32_t> unique;
        for (const auto id : config.instruments) {
            if (id == 0 || id == std::numeric_limits<std::uint32_t>::max() || !unique.insert(id).second)
                throw std::invalid_argument("instrument IDs must be unique nonzero non-null values");
        }
        Engine::CoreSet cores;
        cores.set(static_cast<Actor::CoreId>(core));
        Engine::StartSequence sequence(cores);
        sequence.setExceptionHandler(exceptions);
        const ActorConfig actors{std::move(config), shared};
        sequence.addServiceActor<MatchingService, MatchingEngineActor>(static_cast<Actor::CoreId>(core), actors);
        // The network actor is ordinary so a bind/constructor failure cannot break
        // Simplx's reverse-order service shutdown chain. The ME service outlives it.
        sequence.addActor<OegActor>(static_cast<Actor::CoreId>(core), actors);
        engine = std::make_unique<Engine>(sequence);
        if (shared->failed.load()) throw std::runtime_error(shared->error);
        if (shared->oeg_cpu.load() != static_cast<int>(core) ||
            shared->matching_cpu.load() != static_cast<int>(core))
            throw std::runtime_error("OEG and matching engine did not start on the configured CPU");
    }
};

Exchange::Exchange(ExchangeConfig config) : impl_(std::make_unique<Impl>(std::move(config))) {}
Exchange::~Exchange() = default;
std::uint16_t Exchange::port() const noexcept { return impl_->shared->port.load(); }
Placement Exchange::placement() const noexcept {
    return {impl_->core, impl_->shared->oeg_cpu.load(), impl_->shared->matching_cpu.load()};
}
bool Exchange::failed() const noexcept { return impl_->shared->failed.load(); }
std::string Exchange::error() const {
    std::lock_guard lock(impl_->shared->mutex);
    return impl_->shared->error;
}

} // namespace compute_trade
