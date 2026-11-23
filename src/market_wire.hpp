#pragma once

#include "compute_trade/optiq.hpp"
#include "compute_trade/workers.hpp"

namespace compute_trade::market_wire {

// TCP length prefix, then the little-endian SBE message header. Schema 2
// describes this exchange's market feed separately from its order entry.
constexpr std::uint16_t schema = 2;
constexpr std::uint16_t version = 1;
constexpr std::size_t header_size = 10;

inline void write_header(std::span<std::byte> bytes, std::uint16_t template_id) noexcept {
    using optiq::detail::store;
    store<std::uint16_t>(bytes, 0, bytes.size());
    store<std::uint16_t>(bytes, 2, bytes.size() - header_size);
    store(bytes, 4, template_id);
    store(bytes, 6, schema);
    store(bytes, 8, version);
}

template <std::size_t Body>
auto frame(std::uint16_t template_id) noexcept {
    std::array<std::byte, header_size + Body> bytes{};
    write_header(bytes, template_id);
    return bytes;
}

inline const auto subscribe = frame<0>(1);
inline const auto ready = frame<8>(2); // Initial market sequence is zero.

struct alignas(64) UpdateFrame {
    std::array<std::byte, 64> bytes;
};

inline void encode(UpdateFrame &frame, const MarketUpdate &update,
                   std::uint64_t command_sequence, std::uint64_t sequence) noexcept {
    using optiq::detail::store;
    auto &bytes = frame.bytes;
    write_header(bytes, 3);
    store(bytes, 10, sequence);
    store(bytes, 18, command_sequence);
    store(bytes, 26, static_cast<std::uint8_t>(update.action));
    store(bytes, 27, update.instrument);
    store(bytes, 31, update.order_id);
    store(bytes, 39, update.side);
    store(bytes, 40, update.price);
    store(bytes, 48, update.remaining);
    store(bytes, 56, update.priority);
}

} // namespace compute_trade::market_wire
