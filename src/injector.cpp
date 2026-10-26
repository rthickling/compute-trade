#include "compute_trade/sbe.hpp"
#include "compute_trade/types.hpp"

#include <arpa/inet.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <limits>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>

namespace {

struct Options {
    std::string host{"127.0.0.1"};
    std::uint16_t port{9000};
    std::uint64_t count{10};
    std::uint32_t instrument{1};
    std::int64_t price{10000};
    std::uint64_t quantity{10};
    std::uint64_t window{1};
    bool help{};
};

void usage(std::ostream& out) {
    out << "Usage: ct_injector [options]\n"
           "  --host IPv4       Destination address (default 127.0.0.1)\n"
           "  --port N          TCP port, 1..65535 (default 9000)\n"
           "  --count N         Number of orders, 1..1000000000 (default 10)\n"
           "  --instrument N    Instrument identifier (default 1)\n"
           "  --price N         Positive integer price ticks (default 10000)\n"
           "  --quantity N      Positive whole quantity units (default 10)\n"
           "  --window N        Outstanding orders per batch, 1..1024 (default 1)\n"
           "  --help            Show this help\n\n"
           "Orders alternate buy/sell at the same price. Measurements include TCP\n"
           "and client processing; they are not matching-engine service timings.\n";
}

std::uint64_t number(std::string_view name, std::string_view text,
                     std::uint64_t maximum) {
    std::uint64_t result{};
    const auto parsed = std::from_chars(text.data(), text.data() + text.size(), result);
    if (text.empty() || parsed.ec != std::errc{} ||
        parsed.ptr != text.data() + text.size() || result == 0 || result > maximum) {
        throw std::invalid_argument(std::string(name) + " requires an integer in 1.." +
                                    std::to_string(maximum));
    }
    return result;
}

Options parse_options(int argc, char** argv) {
    Options result;
    for (int index = 1; index < argc; ++index) {
        const std::string_view flag(argv[index]);
        if (flag == "--help") {
            result.help = true;
            continue;
        }
        if (flag != "--host" && flag != "--port" && flag != "--count" &&
            flag != "--instrument" && flag != "--price" && flag != "--quantity" &&
            flag != "--window") {
            throw std::invalid_argument("unknown option: " + std::string(flag));
        }
        if (++index == argc) {
            throw std::invalid_argument("missing value for " + std::string(flag));
        }
        const std::string_view value(argv[index]);
        if (flag == "--host") {
            result.host = value;
        } else if (flag == "--port") {
            result.port = static_cast<std::uint16_t>(number(flag, value, 65535));
        } else if (flag == "--count") {
            result.count = number(flag, value, 1000000000);
        } else if (flag == "--instrument") {
            result.instrument = static_cast<std::uint32_t>(
                number(flag, value, std::numeric_limits<std::uint32_t>::max() - 1));
        } else if (flag == "--price") {
            result.price = static_cast<std::int64_t>(
                number(flag, value, std::numeric_limits<std::int64_t>::max()));
        } else if (flag == "--quantity") {
            result.quantity = number(flag, value, std::numeric_limits<std::uint64_t>::max() - 1);
        } else if (flag == "--window") {
            result.window = number(flag, value, 1024);
        }
    }
    if (result.quantity > std::numeric_limits<std::uint64_t>::max() / result.count) {
        throw std::invalid_argument("count times quantity exceeds the statistics counter range");
    }
    return result;
}

[[noreturn]] void socket_error(std::string_view operation) {
    const int error = errno;
    throw std::runtime_error(std::string(operation) + ": " + std::strerror(error));
}

class Socket {
public:
    explicit Socket(const Options& options) {
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_port = htons(options.port);
        if (::inet_pton(AF_INET, options.host.c_str(), &address.sin_addr) != 1) {
            throw std::invalid_argument("--host must be an IPv4 address");
        }
        fd_ = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
        if (fd_ == -1) {
            socket_error("socket");
        }
        try {
            const timeval timeout{5, 0};
            const int enabled = 1;
            if (::setsockopt(fd_, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) == -1 ||
                ::setsockopt(fd_, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) == -1 ||
                ::setsockopt(fd_, IPPROTO_TCP, TCP_NODELAY, &enabled, sizeof(enabled)) == -1) {
                socket_error("setsockopt");
            }
            if (::connect(fd_, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) == -1) {
                socket_error("connect");
            }
        } catch (...) {
            ::close(fd_);
            throw;
        }
    }

    ~Socket() { ::close(fd_); }
    Socket(const Socket&) = delete;
    Socket& operator=(const Socket&) = delete;

    void send_all(std::span<const std::byte> bytes) const {
        while (!bytes.empty()) {
            const auto sent = ::send(fd_, bytes.data(), bytes.size(), MSG_NOSIGNAL);
            if (sent == -1) {
                if (errno == EINTR) {
                    continue;
                }
                socket_error("send");
            }
            if (sent == 0) {
                throw std::runtime_error("connection closed while sending");
            }
            bytes = bytes.subspan(static_cast<std::size_t>(sent));
        }
    }

    void receive_all(std::span<std::byte> bytes) const {
        while (!bytes.empty()) {
            const auto received = ::recv(fd_, bytes.data(), bytes.size(), 0);
            if (received == -1) {
                if (errno == EINTR) {
                    continue;
                }
                socket_error("recv");
            }
            if (received == 0) {
                throw std::runtime_error("connection closed before all acknowledgements arrived");
            }
            bytes = bytes.subspan(static_cast<std::size_t>(received));
        }
    }

private:
    int fd_{-1};
};

compute_trade::Acknowledgement receive_acknowledgement(const Socket& socket) {
    std::array<std::byte, compute_trade::sbe::max_frame_size> frame{};
    socket.receive_all(std::span(frame).first(compute_trade::sbe::sofh_size));
    const auto length = compute_trade::sbe::frame_length(
        std::span<const std::byte>(frame).first(compute_trade::sbe::sofh_size));
    socket.receive_all(std::span(frame).subspan(compute_trade::sbe::sofh_size,
                                               length - compute_trade::sbe::sofh_size));
    return compute_trade::sbe::decode_acknowledgement(
        std::span<const std::byte>(frame).first(length));
}

int inject(const Options& options) {
    using namespace compute_trade;
    Socket socket(options);
    std::uint64_t accepted{};
    std::uint64_t rejected{};
    std::uint64_t executed{};
    const auto start = std::chrono::steady_clock::now();
    for (std::uint64_t first = 1; first <= options.count;) {
        const auto batch = std::min(options.window, options.count - first + 1);
        for (std::uint64_t index = 0; index < batch; ++index) {
            const auto id = first + index;
            const NewOrder order{id, options.instrument, id % 2 == 1 ? Side::Buy : Side::Sell,
                                 options.price, options.quantity};
            socket.send_all(sbe::encode(order));
        }
        for (std::uint64_t index = 0; index < batch; ++index) {
            const auto ack = receive_acknowledgement(socket);
            if (ack.client_order_id != first + index || ack.instrument_id != options.instrument) {
                throw std::runtime_error("acknowledgement does not identify the expected order");
            }
            if (ack.status == Status::Accepted) {
                if (ack.reason != RejectReason::None || ack.order_id == 0 ||
                    ack.order_id == std::numeric_limits<std::uint64_t>::max() ||
                    ack.remaining_quantity > options.quantity ||
                    ack.executed_quantity != options.quantity - ack.remaining_quantity) {
                    throw std::runtime_error("invalid accepted acknowledgement");
                }
                ++accepted;
                executed += ack.executed_quantity;
            } else if (ack.status == Status::Rejected) {
                if (ack.reason == RejectReason::None || ack.order_id != 0 ||
                    ack.executed_quantity != 0 || ack.remaining_quantity != 0) {
                    throw std::runtime_error("invalid rejected acknowledgement");
                }
                ++rejected;
                std::cerr << "Order " << ack.client_order_id << " rejected, reason="
                          << static_cast<unsigned>(ack.reason) << '\n';
            } else {
                throw std::runtime_error("unknown acknowledgement status");
            }
        }
        first += batch;
    }
    const double elapsed = std::chrono::duration<double>(
        std::chrono::steady_clock::now() - start).count();
    std::cout << "orders=" << options.count << " accepted=" << accepted
              << " rejected=" << rejected << " executed_units=" << executed << '\n'
              << std::fixed << std::setprecision(6)
              << "client_wall_seconds=" << elapsed << '\n'
              << std::setprecision(2)
              << "client_wall_orders_per_second="
              << (elapsed > 0 ? static_cast<double>(options.count) / elapsed : 0) << '\n';
    return rejected == 0 ? 0 : 1;
}

} // namespace

int main(int argc, char** argv) {
    try {
        const auto options = parse_options(argc, argv);
        if (options.help) {
            usage(std::cout);
            return 0;
        }
        return inject(options);
    } catch (const std::exception& error) {
        std::cerr << "ct_injector: " << error.what() << '\n';
        return 1;
    }
}
