#pragma once

#include "compute_trade/types.hpp"
#include "compute_trade/generated/compute_trade_wire/NewOrder.h"
#include "compute_trade/generated/compute_trade_wire/Acknowledgement.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <stdexcept>

namespace compute_trade::sbe {

// Handwritten transport and domain adapter around upstream-generated SBE codecs.
// Only the headers under generated/ are generated from the XML schema.
// SOFH uses network byte order; the SBE message header and body use little endian.
// This is a demo schema, not the Euronext Optiq wire protocol. Decoding is strict
// to version 0: future schema extensions must be implemented explicitly.
inline constexpr std::size_t bits_per_octet = 8;
inline constexpr std::uint32_t octet_mask = 0xFF;
inline constexpr std::size_t sofh_length_offset = 0;
inline constexpr std::size_t sofh_length_size = sizeof(std::uint32_t);
inline constexpr std::size_t sofh_encoding_offset = sofh_length_offset + sofh_length_size;
inline constexpr std::size_t sofh_encoding_size = sizeof(std::uint16_t);
inline constexpr std::size_t sofh_size = sofh_encoding_offset + sofh_encoding_size;
inline constexpr std::size_t header_size = wire::MessageHeader::encodedLength();
inline constexpr std::size_t new_order_frame_size = sofh_size + header_size + wire::NewOrder::sbeBlockLength();
inline constexpr std::size_t acknowledgement_frame_size = sofh_size + header_size + wire::Acknowledgement::sbeBlockLength();
inline constexpr std::size_t max_frame_size = 256;
inline constexpr std::uint16_t schema_id = wire::NewOrder::sbeSchemaId();
inline constexpr std::uint16_t schema_version = wire::NewOrder::sbeSchemaVersion();
inline constexpr std::uint16_t new_order_template_id = wire::NewOrder::sbeTemplateId();
inline constexpr std::uint16_t acknowledgement_template_id = wire::Acknowledgement::sbeTemplateId();
inline constexpr std::uint16_t little_endian_encoding = 0xEB50;
// Wire representation of the SOFH encoding identifier: {0xEB, 0x50}.
// "Little endian" describes the following SBE message, not this identifier:
// SOFH fields always put the most significant byte first (big endian).
// Deriving both bytes from the identifier avoids duplicating its value in the
// writer and validator, which share this array. The shifts and masks produce
// the same bytes on either host byte order, entirely at compile time.
inline constexpr std::array<std::byte, sofh_encoding_size> little_endian_encoding_bytes{
    // Shift the high octet into the low position, then retain only that octet.
    static_cast<std::byte>((little_endian_encoding >> bits_per_octet) & octet_mask),
    // Retain the low octet directly.
    static_cast<std::byte>(little_endian_encoding & octet_mask)
};

using NewOrderFrame = std::array<std::byte, new_order_frame_size>;
using AcknowledgementFrame = std::array<std::byte, acknowledgement_frame_size>;

namespace detail {

// Upstream flyweights take char* for both encoding and decoding. Read paths
// use only const field accessors; no setters or buffer writes are exposed.
inline char* decode_buffer(std::span<const std::byte> bytes) noexcept {
    return const_cast<char*>(reinterpret_cast<const char*>(bytes.data()));
}

// Read the first four bytes as an unsigned big-endian (most significant byte
// first) integer, independently of the host CPU's byte order or alignment.
// For example, 00 00 01 2B becomes 0x0000012B, or 299 decimal.
// Preconditions: bytes.size() >= 4. This helper performs no bounds or protocol
// validation; frame_length() checks that the six-byte SOFH is present first.
// The span borrows the input storage and this function does not modify it.
constexpr std::uint32_t read_be32(std::span<const std::byte> bytes) noexcept {
    std::uint32_t value = 0;
    for (std::size_t i = 0; i < sofh_length_size; ++i) {
        // Make room for the next octet in the low eight bits, then insert it.
        // std::byte needs an explicit integer conversion for this arithmetic.
        value = (value << bits_per_octet) | std::to_integer<unsigned>(bytes[i]);
    }
    return value;
}

// Write the six-byte Simple Open Framing Header into the start of a full frame:
//   [0..3] total frame length, including SOFH, as a big-endian uint32
//   [4..5] encoding identifier 0xEB50, meaning little-endian SBE
// SOFH itself is always big endian, even though the following SBE header and
// body are little endian. For a 43-byte frame, this writes 00 00 00 2B EB 50.
// Preconditions: the span covers the ENTIRE frame, has at least six bytes, and
// its size fits in uint32_t. Passing just the header would encode length 6.
// Our encode() callers supply fixed-size 43- or 52-byte arrays. This helper
// performs no bounds checks, allocates nothing, and leaves bytes [6..] intact.
// See docs/architecture.md, "SOFH framing helpers", for specification links.
inline void write_sofh(std::span<std::byte> bytes) noexcept {
    const auto length = static_cast<std::uint32_t>(bytes.size());
    for (std::size_t i = 0; i < sofh_length_size; ++i) {
        // Shifts of 24, 16, 8, and 0 select each octet from most to least
        // significant; the mask retains only that octet before storing it.
        const auto remaining_octets = sofh_length_size - 1 - i;
        const auto shift = bits_per_octet * remaining_octets;
        bytes[sofh_length_offset + i] = static_cast<std::byte>((length >> shift) & octet_mask);
    }
    for (std::size_t i = 0; i < sofh_encoding_size; ++i) {
        bytes[sofh_encoding_offset + i] = little_endian_encoding_bytes[i];
    }
}

} // namespace detail

// Can be called as soon as six bytes are buffered. The complete frame need not
// have arrived yet; callers retain bytes until the returned length is available.
inline std::size_t frame_length(std::span<const std::byte> bytes) {
    if (bytes.size() < sofh_size) {
        throw std::invalid_argument("truncated SOFH");
    }
    for (std::size_t i = 0; i < sofh_encoding_size; ++i) {
        if (bytes[sofh_encoding_offset + i] != little_endian_encoding_bytes[i]) {
            throw std::invalid_argument("unsupported SOFH encoding");
        }
    }
    const auto length = detail::read_be32(bytes.subspan(sofh_length_offset, sofh_length_size));
    if (length < sofh_size + header_size || length > max_frame_size) {
        throw std::invalid_argument("invalid SOFH message length");
    }
    return length;
}

namespace detail {

inline void check_frame(std::span<const std::byte> bytes,
                        std::uint16_t template_id,
                        std::size_t expected_size) {
    const auto length = frame_length(bytes);
    if (length != bytes.size() || length != expected_size) {
        throw std::invalid_argument("SBE frame length mismatch");
    }
    const wire::MessageHeader header(decode_buffer(bytes), sofh_size, bytes.size(), schema_version);
    if (header.blockLength() != expected_size - sofh_size - header_size) {
        throw std::invalid_argument("unsupported SBE block length");
    }
    if (header.templateId() != template_id) {
        throw std::invalid_argument("unexpected SBE template");
    }
    if (header.schemaId() != schema_id) {
        throw std::invalid_argument("unexpected SBE schema");
    }
    if (header.version() != schema_version) {
        throw std::invalid_argument("unsupported SBE schema version");
    }
}

} // namespace detail

inline NewOrderFrame encode(const NewOrder& order) noexcept {
    NewOrderFrame frame{};
    detail::write_sofh(frame);
    wire::NewOrder message;
    message.wrapAndApplyHeader(reinterpret_cast<char*>(frame.data()), sofh_size, frame.size())
        .clientOrderId(order.client_order_id)
        .instrumentId(order.instrument_id)
        .side(static_cast<wire::Side::Value>(order.side))
        .price(order.price)
        .quantity(order.quantity);
    return frame;
}

inline AcknowledgementFrame encode(const Acknowledgement& acknowledgement) noexcept {
    AcknowledgementFrame frame{};
    detail::write_sofh(frame);
    wire::Acknowledgement message;
    message.wrapAndApplyHeader(reinterpret_cast<char*>(frame.data()), sofh_size, frame.size())
        .clientOrderId(acknowledgement.client_order_id)
        .orderId(acknowledgement.order_id)
        .instrumentId(acknowledgement.instrument_id)
        .status(static_cast<wire::Status::Value>(acknowledgement.status))
        .reason(static_cast<wire::RejectReason::Value>(acknowledgement.reason))
        .executedQuantity(acknowledgement.executed_quantity)
        .remainingQuantity(acknowledgement.remaining_quantity);
    return frame;
}

inline NewOrder decode_new_order(std::span<const std::byte> bytes) {
    detail::check_frame(bytes, new_order_template_id, new_order_frame_size);
    wire::NewOrder message;
    message.wrapForDecode(detail::decode_buffer(bytes), sofh_size + header_size,
                          wire::NewOrder::sbeBlockLength(), schema_version, bytes.size());
    // Preserve invalid side values so the gateway can return InvalidOrder with
    // the original client order id instead of losing business-level context.
    return NewOrder{
        message.clientOrderId(), message.instrumentId(), static_cast<Side>(message.sideRaw()),
        message.price(), message.quantity()
    };
}

inline Acknowledgement decode_acknowledgement(std::span<const std::byte> bytes) {
    detail::check_frame(bytes, acknowledgement_template_id, acknowledgement_frame_size);
    wire::Acknowledgement message;
    message.wrapForDecode(detail::decode_buffer(bytes), sofh_size + header_size,
                          wire::Acknowledgement::sbeBlockLength(), schema_version, bytes.size());
    const auto status = message.statusRaw();
    const auto reason = message.reasonRaw();
    if (status > static_cast<std::uint8_t>(Status::Rejected) ||
        reason > static_cast<std::uint8_t>(RejectReason::DuplicateClientOrderId)) {
        throw std::invalid_argument("invalid acknowledgement enumeration");
    }
    return Acknowledgement{
        message.clientOrderId(), message.orderId(), message.instrumentId(),
        static_cast<Status>(status), static_cast<RejectReason>(reason),
        message.executedQuantity(), message.remainingQuantity()
    };
}

} // namespace compute_trade::sbe
