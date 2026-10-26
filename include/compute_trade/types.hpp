#pragma once

#include <cstdint>

namespace compute_trade {

enum class Side : std::uint8_t { Buy = 1, Sell = 2 };
enum class Status : std::uint8_t { Accepted = 0, Rejected = 1 };
enum class RejectReason : std::uint8_t {
    None = 0,
    MalformedMessage = 1,
    InvalidOrder = 2,
    UnknownInstrument = 3,
    DuplicateClientOrderId = 4
};

// Integer price ticks and whole quantity units; no floating point on the order path.
struct NewOrder {
    std::uint64_t client_order_id{};
    std::uint32_t instrument_id{};
    Side side{Side::Buy};
    std::int64_t price{};
    std::uint64_t quantity{};
};

struct Acknowledgement {
    std::uint64_t client_order_id{};
    std::uint64_t order_id{};
    std::uint32_t instrument_id{};
    Status status{Status::Rejected};
    RejectReason reason{RejectReason::None};
    std::uint64_t executed_quantity{};
    std::uint64_t remaining_quantity{};
};

struct Trade {
    std::uint64_t maker_order_id{};
    std::uint64_t taker_order_id{};
    std::int64_t price{};
    std::uint64_t quantity{};
};

} // namespace compute_trade
