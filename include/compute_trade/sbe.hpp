#pragma once

#include "compute_trade/types.hpp"

#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <span>
#include <stdexcept>
#include <type_traits>

namespace compute_trade::sbe {

// Hand-written codec for the two fixed-size messages in schema/compute-trade.xml.
// SOFH uses network byte order; the SBE message header and body use little endian.
// This is a demo schema, not the Euronext Optiq wire protocol. Decoding is strict
// to version 0: future schema extensions must be implemented explicitly.
inline constexpr std::size_t sofh_size = 6;
inline constexpr std::size_t header_size = 8;
inline constexpr std::size_t new_order_frame_size = 43;
inline constexpr std::size_t acknowledgement_frame_size = 52;
inline constexpr std::size_t max_frame_size = 256;
inline constexpr std::uint16_t schema_id = 1;
inline constexpr std::uint16_t schema_version = 0;
inline constexpr std::uint16_t new_order_template_id = 1;
inline constexpr std::uint16_t acknowledgement_template_id = 2;
inline constexpr std::uint16_t little_endian_encoding = 0xEB50;

using NewOrderFrame = std::array<std::byte, new_order_frame_size>;
using AcknowledgementFrame = std::array<std::byte, acknowledgement_frame_size>;

namespace detail {

template <class T>
constexpr T read_le(std::span<const std::byte> bytes, std::size_t offset) noexcept {
    static_assert(std::is_unsigned_v<T>);
    T value = 0;
    for (std::size_t i = 0; i < sizeof(T); ++i) {
        value |= static_cast<T>(std::to_integer<unsigned>(bytes[offset + i])) << (8 * i);
    }
    return value;
}

template <class T>
constexpr void write_le(std::span<std::byte> bytes, std::size_t offset, T value) noexcept {
    static_assert(std::is_unsigned_v<T>);
    for (std::size_t i = 0; i < sizeof(T); ++i) {
        bytes[offset + i] = static_cast<std::byte>((value >> (8 * i)) & 0xFF);
    }
}

constexpr std::uint32_t read_be32(std::span<const std::byte> bytes) noexcept {
    std::uint32_t value = 0;
    for (std::size_t i = 0; i < 4; ++i) {
        value = (value << 8) | std::to_integer<unsigned>(bytes[i]);
    }
    return value;
}

inline void write_header(std::span<std::byte> bytes, std::uint16_t template_id,
                         std::uint16_t block_length) noexcept {
    const auto length = static_cast<std::uint32_t>(bytes.size());
    for (std::size_t i = 0; i < 4; ++i) {
        bytes[i] = static_cast<std::byte>((length >> (8 * (3 - i))) & 0xFF);
    }
    bytes[4] = std::byte{0xEB};
    bytes[5] = std::byte{0x50};
    write_le<std::uint16_t>(bytes, sofh_size, block_length);
    write_le<std::uint16_t>(bytes, sofh_size + 2, template_id);
    write_le<std::uint16_t>(bytes, sofh_size + 4, schema_id);
    write_le<std::uint16_t>(bytes, sofh_size + 6, schema_version);
}

} // namespace detail

// Can be called as soon as six bytes are buffered. The complete frame need not
// have arrived yet; callers retain bytes until the returned length is available.
inline std::size_t frame_length(std::span<const std::byte> bytes) {
    if (bytes.size() < sofh_size) {
        throw std::invalid_argument("truncated SOFH");
    }
    if (bytes[4] != std::byte{0xEB} || bytes[5] != std::byte{0x50}) {
        throw std::invalid_argument("unsupported SOFH encoding");
    }
    const auto length = detail::read_be32(bytes);
    if (length < sofh_size + header_size || length > max_frame_size) {
        throw std::invalid_argument("invalid SOFH message length");
    }
    return length;
}

namespace detail {

inline std::span<const std::byte> checked_body(std::span<const std::byte> bytes,
                                             std::uint16_t template_id,
                                             std::size_t expected_size) {
    const auto length = frame_length(bytes);
    if (length != bytes.size() || length != expected_size) {
        throw std::invalid_argument("SBE frame length mismatch");
    }
    if (read_le<std::uint16_t>(bytes, sofh_size) != expected_size - sofh_size - header_size) {
        throw std::invalid_argument("unsupported SBE block length");
    }
    if (read_le<std::uint16_t>(bytes, sofh_size + 2) != template_id) {
        throw std::invalid_argument("unexpected SBE template");
    }
    if (read_le<std::uint16_t>(bytes, sofh_size + 4) != schema_id) {
        throw std::invalid_argument("unexpected SBE schema");
    }
    if (read_le<std::uint16_t>(bytes, sofh_size + 6) != schema_version) {
        throw std::invalid_argument("unsupported SBE schema version");
    }
    return bytes.subspan(sofh_size + header_size);
}

} // namespace detail

inline NewOrderFrame encode(const NewOrder& order) noexcept {
    NewOrderFrame frame{};
    detail::write_header(frame, new_order_template_id, 29);
    auto body = std::span(frame).subspan(sofh_size + header_size);
    detail::write_le<std::uint64_t>(body, 0, order.client_order_id);
    detail::write_le<std::uint32_t>(body, 8, order.instrument_id);
    body[12] = static_cast<std::byte>(order.side);
    detail::write_le<std::uint64_t>(body, 13, std::bit_cast<std::uint64_t>(order.price));
    detail::write_le<std::uint64_t>(body, 21, order.quantity);
    return frame;
}

inline AcknowledgementFrame encode(const Acknowledgement& acknowledgement) noexcept {
    AcknowledgementFrame frame{};
    detail::write_header(frame, acknowledgement_template_id, 38);
    auto body = std::span(frame).subspan(sofh_size + header_size);
    detail::write_le<std::uint64_t>(body, 0, acknowledgement.client_order_id);
    detail::write_le<std::uint64_t>(body, 8, acknowledgement.order_id);
    detail::write_le<std::uint32_t>(body, 16, acknowledgement.instrument_id);
    body[20] = static_cast<std::byte>(acknowledgement.status);
    body[21] = static_cast<std::byte>(acknowledgement.reason);
    detail::write_le<std::uint64_t>(body, 22, acknowledgement.executed_quantity);
    detail::write_le<std::uint64_t>(body, 30, acknowledgement.remaining_quantity);
    return frame;
}

inline NewOrder decode_new_order(std::span<const std::byte> bytes) {
    const auto body = detail::checked_body(bytes, new_order_template_id, new_order_frame_size);
    // Preserve invalid side values so the gateway can return InvalidOrder with
    // the original client order id instead of losing business-level context.
    return NewOrder{
        detail::read_le<std::uint64_t>(body, 0),
        detail::read_le<std::uint32_t>(body, 8),
        static_cast<Side>(std::to_integer<std::uint8_t>(body[12])),
        std::bit_cast<std::int64_t>(detail::read_le<std::uint64_t>(body, 13)),
        detail::read_le<std::uint64_t>(body, 21)
    };
}

inline Acknowledgement decode_acknowledgement(std::span<const std::byte> bytes) {
    const auto body = detail::checked_body(bytes, acknowledgement_template_id,
                                           acknowledgement_frame_size);
    const auto status = std::to_integer<std::uint8_t>(body[20]);
    const auto reason = std::to_integer<std::uint8_t>(body[21]);
    if (status > static_cast<std::uint8_t>(Status::Rejected) ||
        reason > static_cast<std::uint8_t>(RejectReason::DuplicateClientOrderId)) {
        throw std::invalid_argument("invalid acknowledgement enumeration");
    }
    return Acknowledgement{
        detail::read_le<std::uint64_t>(body, 0),
        detail::read_le<std::uint64_t>(body, 8),
        detail::read_le<std::uint32_t>(body, 16),
        static_cast<Status>(status),
        static_cast<RejectReason>(reason),
        detail::read_le<std::uint64_t>(body, 22),
        detail::read_le<std::uint64_t>(body, 30)
    };
}

} // namespace compute_trade::sbe
