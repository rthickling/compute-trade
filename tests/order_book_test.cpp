#include "compute_trade/order_book.hpp"

#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using namespace compute_trade;

void check(bool condition, const char* expression, int line) {
    if (!condition) {
        throw std::runtime_error(std::string("line ") + std::to_string(line) + ": " + expression);
    }
}

#define CHECK(expression) check(static_cast<bool>(expression), #expression, __LINE__)

NewOrder order(std::uint64_t client_id, Side side, std::int64_t price,
               std::uint64_t quantity) {
    return NewOrder{client_id, 1, side, price, quantity};
}

void price_time_priority_and_partial_fills() {
    OrderBook book;
    CHECK(!book.best_bid());
    CHECK(!book.best_ask());
    CHECK(book.submit(order(1, Side::Sell, 102, 4), 1).remaining_quantity == 4);
    book.submit(order(2, Side::Sell, 100, 3), 2);
    book.submit(order(3, Side::Sell, 100, 5), 3);
    CHECK(book.best_ask() == 100);
    CHECK(book.resting_order_count() == 3);

    std::vector<Trade> trades;
    const auto ack = book.submit(order(4, Side::Buy, 101, 6), 4,
                                [&](const Trade& trade) { trades.push_back(trade); });
    CHECK(ack.status == Status::Accepted);
    CHECK(ack.reason == RejectReason::None);
    CHECK(ack.client_order_id == 4);
    CHECK(ack.order_id == 4);
    CHECK(ack.instrument_id == 1);
    CHECK(ack.executed_quantity == 6);
    CHECK(ack.remaining_quantity == 0);
    CHECK(trades.size() == 2);
    CHECK(trades[0].maker_order_id == 2);
    CHECK(trades[0].taker_order_id == 4);
    CHECK(trades[0].price == 100);
    CHECK(trades[0].quantity == 3);
    CHECK(trades[1].maker_order_id == 3);
    CHECK(trades[1].price == 100);
    CHECK(trades[1].quantity == 3);
    CHECK(book.quantity_at(Side::Sell, 100) == 2);
    CHECK(book.quantity_at(Side::Sell, 102) == 4);
    CHECK(book.resting_order_count() == 2);
    CHECK(!book.best_bid());

    trades.clear();
    const auto sweep = book.submit(order(5, Side::Buy, 103, 10), 5,
                                  [&](const Trade& trade) { trades.push_back(trade); });
    CHECK(sweep.executed_quantity == 6);
    CHECK(sweep.remaining_quantity == 4);
    CHECK(trades.size() == 2);
    CHECK(trades[0].maker_order_id == 3);
    CHECK(trades[0].price == 100);
    CHECK(trades[1].maker_order_id == 1);
    CHECK(trades[1].price == 102);
    CHECK(!book.best_ask());
    CHECK(book.best_bid() == 103);
    CHECK(book.quantity_at(Side::Buy, 103) == 4);
    CHECK(book.resting_order_count() == 1);
}

void sells_take_highest_bid_first() {
    OrderBook book;
    book.submit(order(1, Side::Buy, 98, 2), 1);
    book.submit(order(2, Side::Buy, 100, 3), 2);
    book.submit(order(3, Side::Buy, 100, 4), 3);
    std::vector<Trade> trades;
    const auto ack = book.submit(order(4, Side::Sell, 99, 8), 4,
                                [&](const Trade& trade) { trades.push_back(trade); });
    CHECK(ack.executed_quantity == 7);
    CHECK(ack.remaining_quantity == 1);
    CHECK(trades.size() == 2);
    CHECK(trades[0].maker_order_id == 2);
    CHECK(trades[1].maker_order_id == 3);
    CHECK(trades[0].price == 100);
    CHECK(trades[1].price == 100);
    CHECK(book.best_bid() == 98);
    CHECK(book.best_ask() == 99);
    CHECK(book.quantity_at(Side::Buy, 98) == 2);
    CHECK(book.quantity_at(Side::Sell, 99) == 1);
    CHECK(book.quantity_at(Side::Sell, 100) == 0);
    CHECK(book.resting_order_count() == 2);

    const auto remainder = book.submit(order(5, Side::Sell, 98, 1), 5);
    CHECK(remainder.executed_quantity == 1);
    CHECK(remainder.remaining_quantity == 0);
    CHECK(book.quantity_at(Side::Buy, 98) == 1);
}

void rejects_invalid_orders_without_mutation() {
    OrderBook book;
    book.submit(order(1, Side::Sell, 100, 10), 1);
    const auto valid = order(2, Side::Buy, 100, 5);
    auto invalid_orders = std::vector<NewOrder>{valid, valid, valid, valid, valid, valid};
    invalid_orders[0].client_order_id = 0;
    invalid_orders[1].side = static_cast<Side>(3);
    invalid_orders[2].price = 0;
    invalid_orders[3].price = -1;
    invalid_orders[4].quantity = 0;
    invalid_orders[5].quantity = std::numeric_limits<std::uint64_t>::max();
    unsigned fill_count = 0;
    for (const auto& invalid : invalid_orders) {
        const auto ack = book.submit(invalid, 2, [&](const Trade&) { ++fill_count; });
        CHECK(ack.status == Status::Rejected);
        CHECK(ack.reason == RejectReason::InvalidOrder);
        CHECK(ack.order_id == 0);
        CHECK(ack.executed_quantity == 0);
        CHECK(ack.remaining_quantity == 0);
        CHECK(book.quantity_at(Side::Sell, 100) == 10);
        CHECK(book.resting_order_count() == 1);
    }
    CHECK(fill_count == 0);
    CHECK(book.submit(valid, 0).status == Status::Rejected);
    auto null_id = valid;
    null_id.client_order_id = std::numeric_limits<std::uint64_t>::max();
    CHECK(book.submit(null_id, 2).status == Status::Rejected);
    CHECK(book.submit(valid, std::numeric_limits<std::uint64_t>::max()).status == Status::Rejected);
}

void copies_resting_order_and_handles_large_integers() {
    OrderBook book;
    const auto large = std::numeric_limits<std::uint64_t>::max() - 1;
    auto source = order(1, Side::Sell, std::numeric_limits<std::int64_t>::max(), large);
    book.submit(source, 1);
    source.price = 1;
    source.quantity = 1;
    CHECK(book.best_ask() == std::numeric_limits<std::int64_t>::max());
    CHECK(book.quantity_at(Side::Sell, std::numeric_limits<std::int64_t>::max()) == large);
    const auto ack = book.submit(order(2, Side::Buy, std::numeric_limits<std::int64_t>::max(), large - 1), 2);
    CHECK(ack.executed_quantity == large - 1);
    CHECK(ack.remaining_quantity == 0);
    CHECK(book.quantity_at(Side::Sell, std::numeric_limits<std::int64_t>::max()) == 1);
    CHECK(book.submit(order(3, Side::Buy, std::numeric_limits<std::int64_t>::max(), 1), 3).executed_quantity == 1);
    CHECK(book.resting_order_count() == 0);
    CHECK(!book.best_ask());
}

struct Allocations {
    std::size_t allocations{};
    std::size_t deallocations{};
    std::size_t live_bytes{};
};

template <class T>
struct CountingAllocator {
    using value_type = T;
    std::shared_ptr<Allocations> state;

    explicit CountingAllocator(std::shared_ptr<Allocations> allocation_state)
        : state(std::move(allocation_state)) {}

    template <class U>
    CountingAllocator(const CountingAllocator<U>& other) noexcept : state(other.state) {}

    T* allocate(std::size_t count) {
        T* allocation = std::allocator<T>{}.allocate(count);
        ++state->allocations;
        state->live_bytes += count * sizeof(T);
        return allocation;
    }

    void deallocate(T* allocation, std::size_t count) noexcept {
        ++state->deallocations;
        state->live_bytes -= count * sizeof(T);
        std::allocator<T>{}.deallocate(allocation, count);
    }

    template <class U>
    bool operator==(const CountingAllocator<U>& other) const noexcept {
        return state == other.state;
    }
};

void propagates_allocator_to_price_levels_and_orders() {
    auto state = std::make_shared<Allocations>();
    {
        OrderBook<CountingAllocator<std::byte>> book{CountingAllocator<std::byte>{state}};
        book.submit(order(1, Side::Buy, 100, 1), 1);
        // One price-level map node and one FIFO order-list node.
        CHECK(state->allocations == 2);
        const auto after_first = state->live_bytes;
        book.submit(order(2, Side::Buy, 100, 1), 2);
        CHECK(state->allocations == 3);
        CHECK(state->live_bytes > after_first);
        book.submit(order(3, Side::Sell, 102, 1), 3);
        CHECK(state->allocations == 5);
        book.submit(order(4, Side::Sell, 100, 2), 4);
        CHECK(state->deallocations == 3);
        CHECK(book.resting_order_count() == 1);
    }
    CHECK(state->live_bytes == 0);
    CHECK(state->allocations == state->deallocations);
}

void aggregate_quantity_overflow_is_explicit() {
    OrderBook book;
    book.submit(order(1, Side::Buy, 100, std::numeric_limits<std::uint64_t>::max() - 1), 1);
    book.submit(order(2, Side::Buy, 100, 2), 2);
    bool threw = false;
    try {
        static_cast<void>(book.quantity_at(Side::Buy, 100));
    } catch (const std::overflow_error&) {
        threw = true;
    }
    CHECK(threw);
}

} // namespace

int main() {
    try {
        price_time_priority_and_partial_fills();
        sells_take_highest_bid_first();
        rejects_invalid_orders_without_mutation();
        copies_resting_order_and_handles_large_integers();
        propagates_allocator_to_price_levels_and_orders();
        aggregate_quantity_overflow_is_explicit();
        std::cout << "order_book_test passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "order_book_test: " << error.what() << '\n';
        return 1;
    }
}
