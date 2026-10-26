#pragma once

#include "compute_trade/types.hpp"

#include <algorithm>
#include <cstddef>
#include <functional>
#include <limits>
#include <list>
#include <map>
#include <memory>
#include <optional>
#include <stdexcept>
#include <utility>

namespace compute_trade {

// A readable price/time-priority baseline. All book allocations use the supplied
// allocator, including price-level nodes and the FIFO order queues. The actor
// supplies its event-loop-local Actor::Allocator; unit tests can use a normal
// allocator. This class and its allocator must stay on their owning thread.
template <class Allocator = std::allocator<std::byte>>
class OrderBook {
    using Price = std::int64_t;

    struct RestingOrder {
        std::uint64_t order_id;
        std::uint64_t remaining;
    };

    template <class T>
    using Rebind = typename std::allocator_traits<Allocator>::template rebind_alloc<T>;

    using OrderAllocator = Rebind<RestingOrder>;
    using Queue = std::list<RestingOrder, OrderAllocator>;
    using LevelAllocator = Rebind<std::pair<const Price, Queue>>;
    using Bids = std::map<Price, Queue, std::greater<Price>, LevelAllocator>;
    using Asks = std::map<Price, Queue, std::less<Price>, LevelAllocator>;

public:
    explicit OrderBook(const Allocator& allocator = {})
        : allocator_(allocator),
          bids_(std::greater<Price>{}, LevelAllocator{allocator}),
          asks_(std::less<Price>{}, LevelAllocator{allocator}) {}

    // The matching-engine actor checks instrument routing and client-ID
    // uniqueness. A book owns exactly one instrument. Its caller assigns IDs.
    // Trade sinks run synchronously; they should not throw. Allocation or sink
    // failure can leave earlier fills applied, so it is a fatal session error.
    template <class TradeSink>
    Acknowledgement submit(const NewOrder& order, std::uint64_t assigned_order_id,
                           TradeSink&& trade_sink) {
        Acknowledgement ack{};
        ack.client_order_id = order.client_order_id;
        ack.instrument_id = order.instrument_id;
        if (!valid(order, assigned_order_id)) {
            ack.reason = RejectReason::InvalidOrder;
            return ack;
        }

        std::uint64_t remaining = order.quantity;
        if (order.side == Side::Buy) {
            match(asks_, order, assigned_order_id, remaining, trade_sink);
            if (remaining != 0) {
                rest(bids_, order.price, assigned_order_id, remaining);
            }
        } else {
            match(bids_, order, assigned_order_id, remaining, trade_sink);
            if (remaining != 0) {
                rest(asks_, order.price, assigned_order_id, remaining);
            }
        }

        ack.order_id = assigned_order_id;
        ack.status = Status::Accepted;
        ack.executed_quantity = order.quantity - remaining;
        ack.remaining_quantity = remaining;
        return ack;
    }

    Acknowledgement submit(const NewOrder& order, std::uint64_t assigned_order_id) {
        return submit(order, assigned_order_id, [](const Trade&) noexcept {});
    }

    [[nodiscard]] std::size_t resting_order_count() const noexcept {
        return resting_count_;
    }

    [[nodiscard]] std::uint64_t quantity_at(Side side, Price price) const {
        if (side == Side::Buy) {
            return quantity_at(bids_, price);
        }
        if (side == Side::Sell) {
            return quantity_at(asks_, price);
        }
        throw std::invalid_argument("unknown order side");
    }

    [[nodiscard]] std::optional<Price> best_bid() const noexcept {
        return best_price(bids_);
    }

    [[nodiscard]] std::optional<Price> best_ask() const noexcept {
        return best_price(asks_);
    }

private:
    static bool valid(const NewOrder& order, std::uint64_t assigned_order_id) noexcept {
        constexpr auto null_u64 = std::numeric_limits<std::uint64_t>::max();
        return (order.side == Side::Buy || order.side == Side::Sell)
            && order.client_order_id != 0 && order.client_order_id != null_u64
            && assigned_order_id != 0 && assigned_order_id != null_u64
            && order.price > 0 && order.quantity != 0 && order.quantity != null_u64;
    }

    template <class Levels, class TradeSink>
    void match(Levels& opposite, const NewOrder& order,
               std::uint64_t assigned_order_id, std::uint64_t& remaining,
               TradeSink& trade_sink) {
        while (remaining != 0 && !opposite.empty()) {
            auto level = opposite.begin();
            const Price maker_price = level->first;
            const bool crosses = order.side == Side::Buy
                ? order.price >= maker_price : order.price <= maker_price;
            if (!crosses) {
                break;
            }

            auto& orders = level->second;
            auto& maker = orders.front();
            const auto quantity = std::min(remaining, maker.remaining);
            const Trade trade{maker.order_id, assigned_order_id, maker_price, quantity};
            maker.remaining -= quantity;
            remaining -= quantity;
            if (maker.remaining == 0) {
                orders.pop_front();
                --resting_count_;
            }
            if (orders.empty()) {
                opposite.erase(level);
            }
            trade_sink(trade);
        }
    }

    template <class Levels>
    void rest(Levels& levels, Price price, std::uint64_t order_id,
              std::uint64_t remaining) {
        // Explicitly pass the allocator into each nested queue. Allocating the
        // map with an actor allocator alone does not redirect list allocations.
        const auto [level, inserted] = levels.try_emplace(price, OrderAllocator{allocator_});
        try {
            level->second.push_back(RestingOrder{order_id, remaining});
        } catch (...) {
            if (inserted) {
                levels.erase(level);
            }
            throw;
        }
        ++resting_count_;
    }

    template <class Levels>
    static std::uint64_t quantity_at(const Levels& levels, Price price) {
        const auto level = levels.find(price);
        if (level == levels.end()) {
            return 0;
        }
        std::uint64_t total = 0;
        for (const auto& order : level->second) {
            if (order.remaining > std::numeric_limits<std::uint64_t>::max() - total) {
                throw std::overflow_error("price-level quantity exceeds uint64");
            }
            total += order.remaining;
        }
        return total;
    }

    template <class Levels>
    static std::optional<Price> best_price(const Levels& levels) noexcept {
        if (levels.empty()) {
            return std::nullopt;
        }
        return levels.begin()->first;
    }

    Allocator allocator_;
    Bids bids_;
    Asks asks_;
    std::size_t resting_count_{};
};

} // namespace compute_trade
