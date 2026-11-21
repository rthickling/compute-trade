#include "compute_trade/optiq.hpp"
#include "compute_trade/sbe.hpp"
#include "compute_trade/types.hpp"

#include <arpa/inet.h>
#include <json-c/json.h>
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
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <span>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace {

struct Options {
  std::string host{"127.0.0.1"};
  std::uint16_t port{9000};
  std::uint64_t count{10};
  std::uint32_t instrument{1};
  std::int64_t price{10000};
  std::uint64_t quantity{10};
  std::uint64_t window{1};
  std::optional<std::uint32_t> logical_access;
  std::uint16_t partition{1};
  std::string firm{"FIRM1"};
  std::filesystem::path recovery_state;
  std::filesystem::path report;
  std::uint64_t cancel_id{};
  std::uint64_t replace_id{};
  bool replay_only{};
  bool sell{};
  bool help{};
};

void usage(std::ostream &out) {
  out << "Usage: ct_injector [options]\n"
         "  --host IPv4       Destination address (default 127.0.0.1)\n"
         "  --port N          TCP port, 1..65535 (default 9000)\n"
         "  --count N         Number of orders, 1..1000000000 (default 10)\n"
         "  --instrument N    Instrument identifier (default 1)\n"
         "  --price N         Positive integer price ticks (default 10000)\n"
         "  --quantity N      Positive whole quantity units (default 10)\n"
         "  --window N        Outstanding orders per batch, 1..1024 (default "
         "1)\n"
         "  --logical-access N  Select an Optiq session instead of legacy framing\n"
         "  --partition N       Optiq partition (default 1)\n"
         "  --firm CODE         Optiq executing firm (default FIRM1)\n"
         "  --cancel ID         Send a Cancel Request for the exchange Order ID\n"
         "  --replace ID        Send a Cancel Replace with --price and --quantity\n"
         "  --sell              Select sell side for --cancel or --replace\n"
         "  --replay-only       Recover private messages and exit\n"
         "  --recovery-state PATH  Resume and checkpoint processed private messages\n"
         "  --report-json PATH  Write the replay frontier, gaps, and unknown outcomes\n"
         "  --help              Show this help\n\n"
         "New orders alternate buy/sell at one price. Wall timings include TCP and client processing.\n";
}

std::uint64_t number(std::string_view name, std::string_view text,
                     std::uint64_t maximum, std::uint64_t minimum = 1) {
  std::uint64_t result{};
  const auto parsed =
      std::from_chars(text.data(), text.data() + text.size(), result);
  if (text.empty() || parsed.ec != std::errc{} ||
      parsed.ptr != text.data() + text.size() || result < minimum ||
      result > maximum) {
    throw std::invalid_argument(std::string(name) +
                                " requires an integer in " + std::to_string(minimum) + ".." +
                                std::to_string(maximum));
  }
  return result;
}

Options parse_options(int argc, char **argv) {
  Options result;
  bool session_credentials{};
  for (int index = 1; index < argc; ++index) {
    const std::string_view flag(argv[index]);
    if (flag == "--help") {
      result.help = true;
      continue;
    }
    if (flag == "--replay-only") {
      result.replay_only = true;
      continue;
    }
    if (flag == "--sell") {
      result.sell = true;
      continue;
    }
    if (flag != "--host" && flag != "--port" && flag != "--count" &&
        flag != "--instrument" && flag != "--price" && flag != "--quantity" &&
        flag != "--window" && flag != "--logical-access" &&
        flag != "--partition" && flag != "--firm" && flag != "--recovery-state" &&
        flag != "--report-json" && flag != "--cancel" && flag != "--replace") {
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
      result.quantity =
          number(flag, value, std::numeric_limits<std::uint64_t>::max() - 1);
    } else if (flag == "--window") {
      result.window = number(flag, value, 1024);
    } else if (flag == "--logical-access") {
      result.logical_access = static_cast<std::uint32_t>(
          number(flag, value, compute_trade::optiq::null_sequence - 1, 0));
    } else if (flag == "--partition") {
      result.partition = static_cast<std::uint16_t>(number(flag, value, 65534, 0));
      session_credentials = true;
    } else if (flag == "--firm") {
      result.firm = value;
      session_credentials = true;
    } else if (flag == "--recovery-state") {
      result.recovery_state = value;
    } else if (flag == "--report-json") {
      result.report = value;
    } else if (flag == "--cancel") {
      result.cancel_id = number(flag, value, compute_trade::optiq::null_order_id - 1);
    } else if (flag == "--replace") {
      result.replace_id = number(flag, value, compute_trade::optiq::null_order_id - 1);
    }
  }
  if ((result.cancel_id != 0 && result.replace_id != 0) ||
      (result.replay_only && (result.cancel_id != 0 || result.replace_id != 0))) {
    throw std::invalid_argument("choose one of --cancel, --replace or --replay-only");
  }
  if (!result.logical_access && (session_credentials || result.replay_only || result.cancel_id != 0 ||
      result.replace_id != 0 || !result.recovery_state.empty() || !result.report.empty())) {
    throw std::invalid_argument("Optiq options require --logical-access");
  }
  if (result.sell && result.cancel_id == 0 && result.replace_id == 0) {
    throw std::invalid_argument("--sell requires --cancel or --replace");
  }
  if (!result.report.empty() && result.report == result.recovery_state) {
    throw std::invalid_argument("recovery state and report must use distinct paths");
  }
  if (result.quantity >
      std::numeric_limits<std::uint64_t>::max() / result.count) {
    throw std::invalid_argument(
        "count times quantity exceeds the statistics counter range");
  }
  return result;
}

[[noreturn]] void socket_error(std::string_view operation) {
  const int error = errno;
  throw std::runtime_error(std::string(operation) + ": " +
                           std::strerror(error));
}

class Socket {
public:
  explicit Socket(const Options &options) {
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
      if (::setsockopt(fd_, SOL_SOCKET, SO_RCVTIMEO, &timeout,
                       sizeof(timeout)) == -1 ||
          ::setsockopt(fd_, SOL_SOCKET, SO_SNDTIMEO, &timeout,
                       sizeof(timeout)) == -1 ||
          ::setsockopt(fd_, IPPROTO_TCP, TCP_NODELAY, &enabled,
                       sizeof(enabled)) == -1) {
        socket_error("setsockopt");
      }
      if (::connect(fd_, reinterpret_cast<const sockaddr *>(&address),
                    sizeof(address)) == -1) {
        socket_error("connect");
      }
    } catch (...) {
      ::close(fd_);
      throw;
    }
  }

  ~Socket() { ::close(fd_); }
  Socket(const Socket &) = delete;
  Socket &operator=(const Socket &) = delete;

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
        throw std::runtime_error(
            "connection closed before all acknowledgements arrived");
      }
      bytes = bytes.subspan(static_cast<std::size_t>(received));
    }
  }

private:
  int fd_{-1};
};

compute_trade::Acknowledgement receive_acknowledgement(const Socket &socket) {
  std::array<std::byte, compute_trade::sbe::max_frame_size> frame{};
  socket.receive_all(std::span(frame).first(compute_trade::sbe::sofh_size));
  const auto length = compute_trade::sbe::frame_length(
      std::span<const std::byte>(frame).first(compute_trade::sbe::sofh_size));
  socket.receive_all(std::span(frame).subspan(
      compute_trade::sbe::sofh_size, length - compute_trade::sbe::sofh_size));
  return compute_trade::sbe::decode_acknowledgement(
      std::span<const std::byte>(frame).first(length));
}

int inject(const Options &options) {
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
      const NewOrder order{id, options.instrument,
                           id % 2 == 1 ? Side::Buy : Side::Sell, options.price,
                           options.quantity};
      socket.send_all(sbe::encode(order));
    }
    for (std::uint64_t index = 0; index < batch; ++index) {
      const auto ack = receive_acknowledgement(socket);
      if (ack.client_order_id != first + index ||
          ack.instrument_id != options.instrument) {
        throw std::runtime_error(
            "acknowledgement does not identify the expected order");
      }
      if (ack.status == Status::Accepted) {
        if (ack.reason != RejectReason::None || ack.order_id == 0 ||
            ack.order_id == std::numeric_limits<std::uint64_t>::max() ||
            ack.remaining_quantity > options.quantity ||
            ack.executed_quantity !=
                options.quantity - ack.remaining_quantity) {
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
        std::cerr << "Order " << ack.client_order_id
                  << " rejected, reason=" << static_cast<unsigned>(ack.reason)
                  << '\n';
      } else {
        throw std::runtime_error("unknown acknowledgement status");
      }
    }
    first += batch;
  }
  const double elapsed =
      std::chrono::duration<double>(std::chrono::steady_clock::now() - start)
          .count();
  std::cout << "orders=" << options.count << " accepted=" << accepted
            << " rejected=" << rejected << " executed_units=" << executed
            << '\n'
            << std::fixed << std::setprecision(6)
            << "client_wall_seconds=" << elapsed << '\n'
            << std::setprecision(2) << "client_wall_orders_per_second="
            << (elapsed > 0 ? static_cast<double>(options.count) / elapsed : 0)
            << '\n';
  return rejected == 0 ? 0 : 1;
}

namespace optiq = compute_trade::optiq;

optiq::Message receive_optiq(const Socket &socket) {
  optiq::Frame frame;
  auto bytes = std::span(frame.bytes);
  socket.receive_all(bytes.first(optiq::header_size));
  frame.size = optiq::frame_length(bytes.first(optiq::header_size));
  socket.receive_all(bytes.subspan(optiq::header_size, frame.size - optiq::header_size));
  return optiq::decode(frame.view());
}

void send_optiq(const Socket &socket, const optiq::Message &message) {
  socket.send_all(optiq::encode(message).view());
}

constexpr std::size_t recovery_limit = 4096;

struct Recovery {
  std::uint32_t frontier{};
  std::set<std::int64_t> pending;
  std::map<std::int64_t, std::uint64_t> orders;
  std::vector<std::pair<std::uint32_t, std::uint32_t>> gaps;
  std::vector<std::uint64_t> notices;
};

json_object *field(json_object *object, const char *name, json_type type, bool optional = false) {
  if (!json_object_is_type(object, json_type_object)) {
    throw std::invalid_argument("expected a JSON recovery object");
  }
  json_object *value{};
  if (!json_object_object_get_ex(object, name, &value)) {
    if (optional) {
      return nullptr;
    }
    throw std::invalid_argument(std::string("missing recovery field ") + name);
  }
  if (!json_object_is_type(value, type)) {
    throw std::invalid_argument(std::string("wrong recovery field type for ") + name);
  }
  return value;
}

std::uint32_t recovery_sequence(json_object *value) {
  const auto number = json_object_get_int64(value);
  if (number < 0 || json_object_get_uint64(value) >= optiq::null_sequence) {
    throw std::invalid_argument("invalid recovery sequence");
  }
  return static_cast<std::uint32_t>(number);
}

std::int64_t recovery_client_id(json_object *value) {
  const auto number = json_object_get_int64(value);
  if (!json_object_is_type(value, json_type_int) || number == optiq::null_client_order_id ||
      (number >= 0 && json_object_get_uint64(value) >
                          static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))) {
    throw std::invalid_argument("invalid recovery Client Order ID");
  }
  return number;
}

std::uint64_t recovery_order_id(json_object *value) {
  const auto number = json_object_get_uint64(value);
  if (!json_object_is_type(value, json_type_int) || json_object_get_int64(value) <= 0 ||
      number == optiq::null_order_id) {
    throw std::invalid_argument("invalid recovery Order ID");
  }
  return number;
}

template <class Function>
void read_array(json_object *object, const char *name, Function visit, bool optional = false) {
  auto *values = field(object, name, json_type_array, optional);
  if (values == nullptr) {
    return;
  }
  const auto count = json_object_array_length(values);
  if (count > recovery_limit) {
    throw std::invalid_argument(std::string("recovery array exceeds limit: ") + name);
  }
  for (std::size_t i = 0; i < count; ++i) {
    visit(json_object_array_get_idx(values, i));
  }
}

Recovery read_recovery(const Options &options) {
  Recovery state;
  if (options.recovery_state.empty() || !std::filesystem::exists(options.recovery_state)) {
    return state;
  }
  std::ifstream input(options.recovery_state, std::ios::binary);
  if (!input) {
    throw std::runtime_error("could not read recovery state");
  }
  constexpr std::size_t file_limit = 1U << 20;
  std::string contents(file_limit + 1, '\0');
  input.read(contents.data(), contents.size());
  contents.resize(static_cast<std::size_t>(input.gcount()));
  if (input.bad() || contents.size() > file_limit) {
    throw std::invalid_argument("invalid or oversized recovery state");
  }
  const std::unique_ptr<json_tokener, decltype(&json_tokener_free)>
      parser(json_tokener_new(), json_tokener_free);
  if (!parser) {
    throw std::bad_alloc();
  }
  json_tokener_set_flags(parser.get(), JSON_TOKENER_STRICT);
  const std::unique_ptr<json_object, decltype(&json_object_put)>
      object(json_tokener_parse_ex(parser.get(), contents.c_str(), static_cast<int>(contents.size() + 1)),
             json_object_put);
  if (!object || json_tokener_get_error(parser.get()) != json_tokener_success ||
      !json_object_is_type(object.get(), json_type_object)) {
    throw std::invalid_argument("invalid JSON recovery state");
  }
  state.frontier = recovery_sequence(field(object.get(), "last_processed", json_type_int));
  read_array(object.get(), "pending_client_order_ids", [&](auto *value) {
    if (!state.pending.insert(recovery_client_id(value)).second) {
      throw std::invalid_argument("duplicate pending Client Order ID");
    }
  });
  read_array(object.get(), "known_orders", [&](auto *value) {
    const auto cid = recovery_client_id(field(value, "client_order_id", json_type_int));
    const auto id = recovery_order_id(field(value, "order_id", json_type_int));
    if (!state.orders.emplace(cid, id).second) {
      throw std::invalid_argument("duplicate known Client Order ID");
    }
  }, true);
  read_array(object.get(), "gaps", [&](auto *value) {
    const auto from = recovery_sequence(field(value, "first_sequence", json_type_int));
    const auto through = recovery_sequence(field(value, "last_sequence", json_type_int));
    if (from == 0 || from > through || through > state.frontier ||
        (!state.gaps.empty() && state.gaps.back().second >= from)) {
      throw std::invalid_argument("recovery gaps must be ordered within the processed frontier");
    }
    state.gaps.emplace_back(from, through);
  }, true);
  read_array(object.get(), "disconnect_order_ids", [&](auto *value) {
    state.notices.push_back(recovery_order_id(value));
  }, true);
  return state;
}

template <class Range, class Write>
void json_array(std::ostream &out, const Range &values, Write write) {
  out << '[';
  bool first = true;
  for (const auto &value : values) {
    if (!first) {
      out << ',';
    }
    first = false;
    write(out, value);
  }
  out << ']';
}

std::string recovery_json(const Recovery &state) {
  const auto number = [](std::ostream &out, auto value) { out << value; };
  std::ostringstream out;
  out << "{\n  \"last_processed\": " << state.frontier
      << ",\n  \"recovery_complete\": "
      << (state.gaps.empty() && state.pending.empty() ? "true" : "false")
      << ",\n  \"pending_client_order_ids\": ";
  json_array(out, state.pending, number);
  out << ",\n  \"unknown_client_order_ids\": ";
  json_array(out, state.pending, number);
  out << ",\n  \"known_orders\": ";
  json_array(out, state.orders, [](std::ostream &json, const auto &order) {
    json << "{\"client_order_id\":" << order.first << ",\"order_id\":" << order.second << '}';
  });
  out << ",\n  \"gaps\": ";
  json_array(out, state.gaps, [](std::ostream &json, const auto &gap) {
    json << "{\"first_sequence\":" << gap.first << ",\"last_sequence\":" << gap.second << '}';
  });
  out << ",\n  \"disconnect_order_ids\": ";
  json_array(out, state.notices, number);
  out << "\n}\n";
  return out.str();
}

void checkpoint(const std::filesystem::path &target, std::string_view contents) {
  if (target.empty()) {
    return;
  }
  std::string temporary = target.string() + ".XXXXXX";
  const int fd = ::mkstemp(temporary.data());
  if (fd == -1) {
    socket_error("create recovery output");
  }
  try {
    while (!contents.empty()) {
      const auto size = ::write(fd, contents.data(), contents.size());
      if (size < 0 && errno == EINTR) {
        continue;
      }
      if (size <= 0) {
        socket_error("write recovery output");
      }
      contents.remove_prefix(static_cast<std::size_t>(size));
    }
  } catch (...) {
    ::close(fd);
    ::unlink(temporary.c_str());
    throw;
  }
  if (::close(fd) == -1 || ::rename(temporary.c_str(), target.c_str()) == -1) {
    const auto failure = errno;
    ::unlink(temporary.c_str());
    errno = failure;
    socket_error("publish recovery output");
  }
}

void record_private(Recovery &state, const optiq::Message &message) {
  using optiq::Template;
  if (message.type != Template::Ack && message.type != Template::Reject &&
      message.type != Template::Fill && message.type != Template::Kill) {
    throw std::runtime_error("unexpected message on the private stream");
  }
  if (message.sequence == 0) {
    throw std::runtime_error("private application sequence must be positive");
  }
  if (message.sequence <= state.frontier) {
    return;
  }
  if (message.sequence == optiq::null_sequence || message.sequence != state.frontier + 1) {
    throw std::runtime_error("unreported gap in the private stream");
  }
  switch (message.type) {
  case Template::Ack:
    if (!state.orders.contains(message.client_order_id) && state.orders.size() == recovery_limit) {
      throw std::runtime_error("client Order ID mapping capacity reached");
    }
    state.orders[message.client_order_id] = message.order_id;
    state.pending.erase(message.client_order_id);
    break;
  case Template::Reject:
    state.pending.erase(message.client_order_id);
    break;
  case Template::Kill:
    state.pending.erase(message.client_order_id);
    if (message.kill_reason == optiq::disconnect_cancel) {
      if (state.notices.size() == recovery_limit) {
        throw std::runtime_error("saved disconnect notice capacity reached");
      }
      state.notices.push_back(message.order_id);
    }
    [[fallthrough]];
  case Template::Fill:
    if (message.type == Template::Kill || message.leaves == 0) {
      std::erase_if(state.orders, [&](const auto &entry) { return entry.second == message.order_id; });
    }
    break;
  default: break;
  }
  state.frontier = message.sequence;
}

std::uint32_t replay(const Socket &socket, const Options &options, Recovery &state) {
  using optiq::Template;
  send_optiq(socket, {.type = Template::Logon, .logical_access_id = *options.logical_access,
                       .partition_id = options.partition, .last_sequence = state.frontier});
  const auto answer = receive_optiq(socket);
  if (answer.type != Template::LogonAck || answer.sequence == optiq::null_sequence) {
    throw std::runtime_error("Optiq logon failed");
  }
  for (;;) {
    const auto message = receive_optiq(socket);
    if (message.type == Template::ReplayComplete) {
      if (message.sequence != state.frontier) {
        throw std::runtime_error("Replay Complete does not match the processed frontier");
      }
      return answer.sequence;
    }
    if (message.type == Template::ReplayGap) {
      if (message.first_sequence != state.frontier + 1 || state.gaps.size() == recovery_limit) {
        throw std::runtime_error("invalid or excessive reported replay gaps");
      }
      state.gaps.emplace_back(message.first_sequence, message.last_sequence);
      state.frontier = message.last_sequence;
    } else {
      record_private(state, message);
    }
    if (!options.recovery_state.empty()) {
      checkpoint(options.recovery_state, recovery_json(state));
    }
  }
}

int inject_optiq(const Options &options) {
  using optiq::Template;
  using compute_trade::Side;
  const auto firm = optiq::firm_id(options.firm);
  auto state = read_recovery(options);
  Socket socket(options);
  auto client_sequence = replay(socket, options, state);
  checkpoint(options.report, recovery_json(state));
  if (state.frontier != 0) {
    send_optiq(socket, {.type = Template::Confirm, .sequence = state.frontier});
  }
  if (options.replay_only) {
    if (state.gaps.empty() && state.pending.empty()) {
      std::cout << "recovery_complete=true last_processed=" << state.frontier << '\n';
      return 0;
    }
    std::cout << "recovery_complete=false last_processed=" << state.frontier << '\n';
    return 2;
  }

  const auto type = options.cancel_id != 0 ? Template::CancelRequest :
                    options.replace_id != 0 ? Template::CancelReplace : Template::NewOrder;
  const auto count = type == Template::NewOrder ? options.count : std::uint64_t{1};
  if (count >= optiq::null_sequence - client_sequence) {
    throw std::invalid_argument("orders exceed the remaining inbound sequence range");
  }
  std::uint64_t accepted{};
  std::uint64_t rejected{};
  for (std::uint64_t first = 0; first < count;) {
    const auto batch = std::min(options.window, count - first);
    for (std::uint64_t index = 0; index < batch; ++index) {
      const auto sequence = ++client_sequence;
      const auto cid = static_cast<std::int64_t>(sequence);
      if (state.pending.size() == recovery_limit || !state.pending.insert(cid).second ||
          state.orders.contains(cid)) {
        throw std::runtime_error("client state cannot reserve a new Client Order ID");
      }
      if (!options.recovery_state.empty()) {
        checkpoint(options.recovery_state, recovery_json(state));
      }
      const auto side = (type == Template::NewOrder ? ((first + index) % 2) != 0 : options.sell)
                            ? Side::Sell : Side::Buy;
      const auto order_id = type == Template::NewOrder ? optiq::null_order_id :
                            type == Template::CancelRequest ? options.cancel_id : options.replace_id;
      const auto now = std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::system_clock::now().time_since_epoch()).count();
      send_optiq(socket, {.type = type, .sequence = sequence, .firm = firm,
                          .client_order_id = cid, .order_id = order_id, .instrument = options.instrument,
                          .side = side, .price = options.price, .quantity = options.quantity,
                          .time = static_cast<std::uint64_t>(now)});
    }
    const auto batch_start_sequence = static_cast<std::uint32_t>(client_sequence - batch + 1);
    for (std::uint64_t i = 0; i < batch; ++i) {
      for (;;) {
        const auto response = receive_optiq(socket);
        record_private(state, response);
        if (!options.recovery_state.empty()) {
          checkpoint(options.recovery_state, recovery_json(state));
        }
        if (response.type == Template::Fill) {
          continue;
        }
        const bool expected_type = response.type == Template::Reject ||
            response.type == (type == Template::CancelRequest ? Template::Kill : Template::Ack);
        if (!expected_type ||
            response.client_order_id != static_cast<std::int64_t>(batch_start_sequence + i)) {
          throw std::runtime_error("private reply does not identify the expected command");
        }
        if (response.type == Template::Reject) {
          ++rejected;
          std::cerr << "command " << response.client_order_id
                    << " rejected, error=" << response.error_code << '\n';
        } else {
          ++accepted;
          std::cout << "client_order_id=" << response.client_order_id
                    << " order_id=" << response.order_id << '\n';
        }
        break;
      }
    }
    send_optiq(socket, {.type = Template::Confirm, .sequence = state.frontier});
    first += batch;
  }
  checkpoint(options.report, recovery_json(state));
  std::cout << "orders=" << count << " accepted=" << accepted << " rejected=" << rejected << '\n';
  return rejected == 0 ? 0 : 1;
}

} // namespace

int main(int argc, char **argv) {
  try {
    const auto options = parse_options(argc, argv);
    if (options.help) {
      usage(std::cout);
      return 0;
    }
    return options.logical_access ? inject_optiq(options) : inject(options);
  } catch (const std::exception &error) {
    std::cerr << "ct_injector: " << error.what() << '\n';
    return 1;
  }
}
