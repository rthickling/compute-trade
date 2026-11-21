#pragma once

#include "compute_trade/types.hpp"

#include <algorithm>
#include <array>
#include <bit>
#include <concepts>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>
#include <stdexcept>
#include <string_view>
#include <type_traits>

namespace compute_trade::optiq {

// Selected v354 OEG messages. Wire offsets and group widths follow Euronext's
// oeg_binary_sbe_input_354.xml and OEG Interface Specification v5.354.0.
// Compute Trade's session-control messages use a separate schema.
enum class Template : std::uint16_t {
  NewOrder = 1,
  Ack = 3,
  Fill = 4,
  Kill = 5,
  CancelReplace = 6,
  Reject = 7,
  CancelRequest = 12,
  Logon = 100,
  LogonAck = 101,
  LogonReject = 102,
  Confirm = 200,
  ReplayGap = 201,
  ReplayComplete = 202
};

constexpr std::size_t header_size = 10;
constexpr std::size_t max_frame_size = 256;
constexpr std::int64_t null_client_order_id =
    std::numeric_limits<std::int64_t>::min();
constexpr std::uint64_t null_order_id =
    std::numeric_limits<std::uint64_t>::max();
constexpr std::uint32_t null_sequence =
    std::numeric_limits<std::uint32_t>::max();
constexpr std::uint8_t limit_order = 2;
constexpr std::uint16_t client_cancel = 1;
constexpr std::uint16_t disconnect_cancel = 11;

using Firm = std::array<char, 8>;

inline Firm firm_id(std::string_view text) {
  if (text.empty() || text.size() > Firm{}.size() ||
      std::ranges::any_of(
          text, [](unsigned char ch) { return ch == 0 || ch > 127; })) {
    throw std::invalid_argument(
        "Firm ID must have one to eight non-NUL ASCII bytes");
  }
  Firm result{};
  std::ranges::copy(text, result.begin());
  return result;
}

// For a Fill, price and quantity are the execution price and executed units.
// For Logon Ack, sequence is the inbound client watermark. For Logon and
// Replay Gap, last_sequence is the requested watermark or inclusive gap end.
struct Message {
  Template type{};
  std::uint32_t sequence{};
  Firm firm{};
  std::int64_t client_order_id{null_client_order_id};
  std::int64_t original_client_order_id{null_client_order_id};
  std::uint64_t order_id{null_order_id};
  std::uint32_t instrument{};
  Side side{Side::Buy};
  std::int64_t price{};
  std::uint64_t quantity{};
  std::uint64_t priority{null_order_id};
  std::uint64_t leaves{};
  std::uint64_t time{};
  std::uint64_t book_in_time{null_order_id};
  std::uint8_t order_type{limit_order};
  std::uint8_t time_in_force{};
  std::uint8_t execution_instruction{};
  std::uint8_t ack_type{};
  std::uint8_t ack_qualifiers{};
  std::uint8_t logon_reject_code{1};
  std::uint8_t emm{1};
  std::uint8_t dark_execution_instruction{};
  std::uint8_t order_category{1};
  std::uint8_t trade_qualifier{};
  std::uint8_t queueing_indicator{1};
  std::uint16_t kill_reason{};
  std::uint16_t error_code{};
  std::uint16_t rejected_message_id{std::numeric_limits<std::uint16_t>::max()};
  std::uint32_t logical_access_id{};
  std::uint16_t partition_id{};
  std::uint32_t last_sequence{};
  std::uint32_t first_sequence{};
  std::uint32_t execution_id{};
  bool has_optional_groups{};
};

struct Frame {
  std::array<std::byte, max_frame_size> bytes{};
  std::size_t size{};

  std::span<const std::byte> view() const {
    return std::span<const std::byte>(bytes).first(size);
  }
};

namespace detail {

template <std::integral T>
T load(std::span<const std::byte> bytes, std::size_t offset) {
  std::make_unsigned_t<T> bits{};
  for (std::size_t i = 0; i < sizeof(T); ++i) {
    bits |= static_cast<std::make_unsigned_t<T>>(
        static_cast<std::uint64_t>(std::to_integer<unsigned>(bytes[offset + i]))
        << (8 * i));
  }
  return std::bit_cast<T>(bits);
}

template <std::integral T>
void store(std::span<std::byte> bytes, std::size_t offset, T value) {
  const auto bits = std::bit_cast<std::make_unsigned_t<T>>(value);
  for (std::size_t i = 0; i < sizeof(T); ++i) {
    bytes[offset + i] = static_cast<std::byte>((bits >> (8 * i)) & 0xff);
  }
}

inline void store_firm(std::span<std::byte> bytes, const Firm &firm) {
  for (std::size_t i = 0; i < firm.size(); ++i) {
    bytes[i] = static_cast<std::byte>(firm[i]);
  }
}

inline Firm load_firm(std::span<const std::byte> bytes) {
  Firm firm{};
  for (std::size_t i = 0; i < firm.size(); ++i) {
    firm[i] = static_cast<char>(bytes[i]);
  }
  return firm;
}

struct Layout {
  std::uint16_t size;
  std::span<const std::uint8_t> groups{};
  std::uint16_t schema{};
  std::uint16_t version{354};
};

inline constexpr std::uint8_t new_order_groups[]{18, 12, 50, 35, 0, 0, 16, 4};
inline constexpr std::uint8_t ack_kill_groups[]{9};
inline constexpr std::uint8_t fill_groups[]{32, 41, 9, 17};
inline constexpr std::uint8_t replace_groups[]{18, 34, 33, 0, 0, 16};
inline constexpr std::uint8_t reject_groups[]{9, 9};
inline constexpr std::uint8_t cancel_groups[]{0, 0};
inline constexpr Firm exchange_id{'C', 'T'};
constexpr std::uint8_t null_byte = std::numeric_limits<std::uint8_t>::max();
constexpr std::uint16_t null_short = std::numeric_limits<std::uint16_t>::max();
constexpr std::int32_t null_int = std::numeric_limits<std::int32_t>::min();

inline Layout layout(Template type) {
  switch (type) {
  case Template::NewOrder:
    return {74, new_order_groups};
  case Template::Ack:
    return {133, ack_kill_groups};
  case Template::Fill:
    return {118, fill_groups};
  case Template::Kill:
    return {100, ack_kill_groups};
  case Template::CancelReplace:
    return {83, replace_groups};
  case Template::Reject:
    return {95, reject_groups};
  case Template::CancelRequest:
    return {60, cancel_groups};
  case Template::Logon:
    return {19};
  case Template::LogonAck:
    return {12};
  case Template::LogonReject:
    return {17};
  case Template::Confirm:
  case Template::ReplayComplete:
    return {4, {}, 1, 1};
  case Template::ReplayGap:
    return {8, {}, 1, 1};
  }
  throw std::invalid_argument("unsupported OEG template ID");
}

inline void private_common(std::span<std::byte> body, const Message &message) {
  store(body, 0, message.sequence);
  store_firm(body.subspan(4), message.firm);
  store(body, 12, message.time);
}

inline void private_reply(std::span<std::byte> body, const Message &message) {
  private_common(body, message);
  store(body, 20, null_order_id);
  store(body, 28, null_order_id);
  store(body, 36, message.book_in_time);
  store(body, 44, null_order_id);
  store(body, 52, null_order_id);
  store(body, 60, null_order_id);
  store(body, 68, message.client_order_id);
}

inline void read_private_common(Message &message,
                                std::span<const std::byte> body) {
  message.sequence = load<std::uint32_t>(body, 0);
  message.firm = load_firm(body.subspan(4));
  message.time = load<std::uint64_t>(body, 12);
}

inline void request_instrument(std::span<std::byte> body, std::size_t offset,
                               const Message &message) {
  store(body, offset, message.instrument);
  store(body, offset + 4, message.emm);
  store(body, offset + 5, static_cast<std::uint8_t>(message.side));
  store(body, offset + 6, message.order_type);
}

inline void read_request_instrument(Message &message,
                                    std::span<const std::byte> body,
                                    std::size_t offset) {
  message.instrument = load<std::uint32_t>(body, offset);
  message.emm = load<std::uint8_t>(body, offset + 4);
  message.side = static_cast<Side>(load<std::uint8_t>(body, offset + 5));
  message.order_type = load<std::uint8_t>(body, offset + 6);
}

} // namespace detail

inline std::size_t frame_length(std::span<const std::byte> bytes) {
  if (bytes.size() < header_size) {
    throw std::invalid_argument("incomplete OEG header");
  }
  const auto length = detail::load<std::uint16_t>(bytes, 0);
  if (length < header_size || length > max_frame_size) {
    throw std::invalid_argument("invalid OEG frame length");
  }
  return length;
}

inline Frame encode(const Message &message) {
  using namespace detail;
  const auto description = layout(message.type);
  Frame frame;
  frame.size = header_size + description.size + 2 * description.groups.size();
  const auto bytes = std::span(frame.bytes);
  store(bytes, 0, static_cast<std::uint16_t>(frame.size));
  store(bytes, 2, description.size);
  store(bytes, 4, static_cast<std::uint16_t>(message.type));
  store(bytes, 6, description.schema);
  store(bytes, 8, description.version);
  auto body = bytes.subspan(header_size, description.size);
  auto group_offset = header_size + description.size;
  for (const auto group : description.groups) {
    store(bytes, group_offset, group);
    group_offset += 2;
  }

  switch (message.type) {
  case Template::NewOrder:
    private_common(body, message);
    store(body, 20, message.client_order_id);
    request_instrument(body, 28, message);
    store(body, 35, message.time_in_force);
    store(body, 36, message.price);
    store(body, 44, message.quantity);
    store(body, 52, std::int32_t{1});
    store(body, 56, std::uint8_t{1});
    store(body, 57, std::uint8_t{2});
    store(body, 58, null_byte);
    store(body, 59, message.execution_instruction);
    store(body, 60, message.dark_execution_instruction);
    store(body, 62, null_short);
    store(body, 64, null_short);
    store(body, 66, null_client_order_id);
    break;
  case Template::CancelRequest:
  case Template::CancelReplace:
    private_common(body, message);
    store(body, 20, std::int32_t{1});
    store(body, 24, null_int);
    store(body, 28, message.client_order_id);
    store(body, 36, message.order_id);
    store(body, 44, message.original_client_order_id);
    if (message.type == Template::CancelRequest) {
      request_instrument(body, 52, message);
      store(body, 59, message.order_category);
    } else {
      store(body, 52, message.price);
      store(body, 60, message.quantity);
      request_instrument(body, 68, message);
      store(body, 75, message.time_in_force);
      store(body, 76, null_byte);
      store(body, 77, null_byte);
      store(body, 78, message.execution_instruction);
      store(body, 79, message.dark_execution_instruction);
      store(body, 81, null_short);
    }
    break;
  case Template::Ack:
    private_reply(body, message);
    store(body, 76, message.original_client_order_id);
    store(body, 84, message.instrument);
    store(body, 88, message.emm);
    store(body, 89, static_cast<std::uint8_t>(message.side));
    store(body, 90, message.ack_type);
    store(body, 91, std::uint8_t{1});
    store(body, 92, message.order_id);
    store(body, 100, message.priority);
    store(body, 108, message.price);
    store(body, 116, message.quantity);
    store(body, 124, message.ack_qualifiers);
    store(body, 125, null_client_order_id);
    break;
  case Template::Fill:
    private_common(body, message);
    store(body, 20, null_order_id);
    store(body, 28, null_order_id);
    store(body, 36, null_order_id);
    store(body, 44, null_client_order_id);
    store(body, 52, message.instrument);
    store(body, 56, message.emm);
    store(body, 57, static_cast<std::uint8_t>(message.side));
    store(body, 58, std::uint8_t{1});
    store(body, 59, message.trade_qualifier);
    store(body, 60, message.order_id);
    store(body, 68, message.price);
    store(body, 76, message.quantity);
    store(body, 84, message.leaves);
    store(body, 92, message.execution_id);
    store(body, 96, std::uint8_t{1});
    store(body, 97, null_sequence);
    store(body, 101, null_byte);
    break;
  case Template::Kill:
    private_reply(body, message);
    store(body, 76, message.original_client_order_id);
    store(body, 84, message.order_id);
    store(body, 92, message.instrument);
    store(body, 96, message.emm);
    store(body, 97, message.kill_reason);
    store(body, 99, message.ack_qualifiers);
    break;
  case Template::Reject:
    private_reply(body, message);
    store(body, 76, message.order_id);
    store(body, 84, message.instrument);
    store(body, 88, message.emm);
    store(body, 89, null_byte);
    store(body, 90, message.error_code);
    store(body, 92, message.rejected_message_id);
    store(body, 94, message.ack_qualifiers);
    break;
  case Template::Logon:
    store(body, 0, message.logical_access_id);
    store(body, 4, message.partition_id);
    store(body, 6, message.last_sequence);
    store(body, 18, message.queueing_indicator);
    break;
  case Template::LogonAck:
    store_firm(body, exchange_id);
    store(body, 8, message.sequence);
    break;
  case Template::LogonReject:
    store_firm(body, exchange_id);
    store(body, 8, message.logon_reject_code);
    store(body, 9, message.sequence);
    store(body, 13, message.last_sequence);
    break;
  case Template::Confirm:
  case Template::ReplayComplete:
    store(body, 0, message.sequence);
    break;
  case Template::ReplayGap:
    store(body, 0, message.first_sequence);
    store(body, 4, message.last_sequence);
    break;
  }
  return frame;
}

inline Message decode(std::span<const std::byte> bytes) {
  using namespace detail;
  const auto length = frame_length(bytes);
  const auto type = static_cast<Template>(load<std::uint16_t>(bytes, 4));
  const auto description = layout(type);
  if (length != bytes.size() || length < header_size + description.size ||
      load<std::uint16_t>(bytes, 2) != description.size ||
      load<std::uint16_t>(bytes, 6) != description.schema ||
      load<std::uint16_t>(bytes, 8) != description.version) {
    throw std::invalid_argument("unexpected OEG block, schema or version");
  }
  const auto body = bytes.subspan(header_size, description.size);
  Message message{.type = type};
  auto position = header_size + description.size;
  for (const auto width : description.groups) {
    if (position + 2 > bytes.size() ||
        load<std::uint8_t>(bytes, position) != width) {
      throw std::invalid_argument("invalid OEG group dimensions");
    }
    const auto count = load<std::uint8_t>(bytes, position + 1);
    position += 2 + static_cast<std::size_t>(width) * count;
    if ((width == 0 && count != 0) || position > bytes.size()) {
      throw std::invalid_argument("invalid OEG group count");
    }
    message.has_optional_groups |= count != 0;
  }
  if (position != bytes.size()) {
    throw std::invalid_argument("trailing OEG bytes");
  }

  switch (type) {
  case Template::NewOrder:
    read_private_common(message, body);
    message.client_order_id = load<std::int64_t>(body, 20);
    read_request_instrument(message, body, 28);
    message.time_in_force = load<std::uint8_t>(body, 35);
    message.price = load<std::int64_t>(body, 36);
    message.quantity = load<std::uint64_t>(body, 44);
    message.execution_instruction = load<std::uint8_t>(body, 59);
    message.dark_execution_instruction = load<std::uint8_t>(body, 60);
    break;
  case Template::CancelRequest:
  case Template::CancelReplace:
    read_private_common(message, body);
    message.client_order_id = load<std::int64_t>(body, 28);
    message.order_id = load<std::uint64_t>(body, 36);
    message.original_client_order_id = load<std::int64_t>(body, 44);
    if (type == Template::CancelRequest) {
      read_request_instrument(message, body, 52);
      message.order_category = load<std::uint8_t>(body, 59);
    } else {
      message.price = load<std::int64_t>(body, 52);
      message.quantity = load<std::uint64_t>(body, 60);
      read_request_instrument(message, body, 68);
      message.time_in_force = load<std::uint8_t>(body, 75);
      message.execution_instruction = load<std::uint8_t>(body, 78);
      message.dark_execution_instruction = load<std::uint8_t>(body, 79);
    }
    break;
  case Template::Ack:
    read_private_common(message, body);
    message.book_in_time = load<std::uint64_t>(body, 36);
    message.client_order_id = load<std::int64_t>(body, 68);
    message.original_client_order_id = load<std::int64_t>(body, 76);
    message.instrument = load<std::uint32_t>(body, 84);
    message.emm = load<std::uint8_t>(body, 88);
    message.side = static_cast<Side>(load<std::uint8_t>(body, 89));
    message.ack_type = load<std::uint8_t>(body, 90);
    message.order_id = load<std::uint64_t>(body, 92);
    message.priority = load<std::uint64_t>(body, 100);
    message.price = load<std::int64_t>(body, 108);
    message.quantity = load<std::uint64_t>(body, 116);
    message.ack_qualifiers = load<std::uint8_t>(body, 124);
    break;
  case Template::Fill:
    read_private_common(message, body);
    message.client_order_id = load<std::int64_t>(body, 44);
    message.instrument = load<std::uint32_t>(body, 52);
    message.emm = load<std::uint8_t>(body, 56);
    message.side = static_cast<Side>(load<std::uint8_t>(body, 57));
    message.trade_qualifier = load<std::uint8_t>(body, 59);
    message.order_id = load<std::uint64_t>(body, 60);
    message.price = load<std::int64_t>(body, 68);
    message.quantity = load<std::uint64_t>(body, 76);
    message.leaves = load<std::uint64_t>(body, 84);
    message.execution_id = load<std::uint32_t>(body, 92);
    break;
  case Template::Kill:
    read_private_common(message, body);
    message.book_in_time = load<std::uint64_t>(body, 36);
    message.client_order_id = load<std::int64_t>(body, 68);
    message.original_client_order_id = load<std::int64_t>(body, 76);
    message.order_id = load<std::uint64_t>(body, 84);
    message.instrument = load<std::uint32_t>(body, 92);
    message.emm = load<std::uint8_t>(body, 96);
    message.kill_reason = load<std::uint16_t>(body, 97);
    message.ack_qualifiers = load<std::uint8_t>(body, 99);
    break;
  case Template::Reject:
    read_private_common(message, body);
    message.book_in_time = load<std::uint64_t>(body, 36);
    message.client_order_id = load<std::int64_t>(body, 68);
    message.order_id = load<std::uint64_t>(body, 76);
    message.instrument = load<std::uint32_t>(body, 84);
    message.emm = load<std::uint8_t>(body, 88);
    message.error_code = load<std::uint16_t>(body, 90);
    message.rejected_message_id = load<std::uint16_t>(body, 92);
    message.ack_qualifiers = load<std::uint8_t>(body, 94);
    break;
  case Template::Logon:
    message.logical_access_id = load<std::uint32_t>(body, 0);
    message.partition_id = load<std::uint16_t>(body, 4);
    message.last_sequence = load<std::uint32_t>(body, 6);
    message.queueing_indicator = load<std::uint8_t>(body, 18);
    break;
  case Template::LogonAck:
    message.sequence = load<std::uint32_t>(body, 8);
    break;
  case Template::LogonReject:
    message.logon_reject_code = load<std::uint8_t>(body, 8);
    message.sequence = load<std::uint32_t>(body, 9);
    message.last_sequence = load<std::uint32_t>(body, 13);
    break;
  case Template::Confirm:
  case Template::ReplayComplete:
    message.sequence = load<std::uint32_t>(body, 0);
    if (message.sequence == null_sequence) {
      throw std::invalid_argument("invalid session-control sequence");
    }
    break;
  case Template::ReplayGap:
    message.first_sequence = load<std::uint32_t>(body, 0);
    message.last_sequence = load<std::uint32_t>(body, 4);
    if (message.first_sequence == 0 ||
        message.first_sequence > message.last_sequence ||
        message.last_sequence == null_sequence) {
      throw std::invalid_argument("invalid replay gap");
    }
    break;
  }
  return message;
}

} // namespace compute_trade::optiq
