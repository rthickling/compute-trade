#include "compute_trade/matcher.hpp"

#include <array>
#include <cstddef>
#include <iostream>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>

namespace {
using namespace compute_trade;

constexpr std::array<char, 8> firm{'F', '1'};
constexpr std::array<char, 8> other_firm{'F', '2'};

#define CHECK(condition)                                                        \
  do {                                                                          \
    if (!(condition))                                                           \
      throw std::runtime_error("line " + std::to_string(__LINE__) + ": " #condition); \
  } while (false)

MatchCommand make_order(std::size_t session, Side side, std::int64_t price,
                        std::uint64_t quantity) {
  return {MatchKind::New, session, firm, 1, side, price, quantity};
}

template <class Book>
std::uint64_t execute(Book &book, MatchPlan &plan, const MatchCommand &order) {
  book.prepare(order, plan);
  CHECK(plan.rejection.empty());
  book.commit(plan);
  return plan.result.order_id;
}

void rejecting_an_oversized_sweep_avoids_partial_execution() {
  Matcher book({1}, 2, {.matching_max_fills = 1});
  auto plan = book.make_plan();
  const auto first = execute(book, plan, make_order(0, Side::Sell, 100, 1));
  const auto second = execute(book, plan, make_order(0, Side::Sell, 100, 1));

  book.prepare(make_order(1, Side::Buy, 100, 2), plan);
  CHECK(plan.rejection == "sweep_capacity");
  CHECK(plan.fills.empty() && plan.changes.empty());
  book.commit(plan);
  const auto unchanged = book.snapshot();
  CHECK(unchanged.size() == 2);
  CHECK(unchanged[0].order_id == first && unchanged[1].order_id == second);
  CHECK(execute(book, plan, make_order(1, Side::Buy, 100, 1)) == second + 1);
  CHECK(plan.fills[0].maker.order_id == first);
}

void a_cross_reuses_capacity_from_removed_makers() {
  Matcher book({1}, 2,
               {.live_orders_total = 1, .live_orders_per_session = 1,
                .active_prices = 1, .matching_max_fills = 1});
  auto plan = book.make_plan();
  const auto maker = execute(book, plan, make_order(0, Side::Sell, 100, 1));

  book.prepare(make_order(1, Side::Buy, 101, 2), plan);
  CHECK(plan.rejection.empty());
  CHECK(plan.reservations_released == 1 && plan.needs_reservation);
  CHECK(plan.changes.size() == 2);
  CHECK(book.snapshot()[0].order_id == maker);

  book.commit(plan);
  const auto orders = book.snapshot();
  CHECK(orders.size() == 1 && orders[0].remaining == 1);
  CHECK(orders[0].order_id != maker && orders[0].side == Side::Buy);
}

void a_reprice_uses_the_origin_reservation() {
  Matcher book({1}, 2, {.active_prices = 1});
  auto plan = book.make_plan();
  const auto maker = execute(book, plan, make_order(0, Side::Sell, 100, 1));
  MatchCommand amend{MatchKind::Replace, 1, other_firm, 1, Side::Sell, 101,
                     1, maker};
  book.prepare(amend, plan);
  CHECK(plan.rejection == "wrong_owner");
  CHECK(book.snapshot()[0].price == 100);

  amend.firm = firm;
  CHECK(execute(book, plan, amend) == maker);
  CHECK(plan.reservations_released == 0 && !plan.needs_reservation);
  CHECK(plan.result.priority == 2 && plan.result.session == 0);
  CHECK(book.snapshot()[0].price == 101);

  MatchCommand disconnect;
  disconnect.kind = MatchKind::Disconnect;
  disconnect.session = 1;
  book.prepare(disconnect, plan);
  CHECK(plan.changes.empty());
  disconnect.session = 0;
  book.prepare(disconnect, plan);
  CHECK(plan.changes.size() == 1 && plan.changes[0].order.order_id == maker);
  book.commit(plan);
  CHECK(book.snapshot().empty());
}

struct AllocationBudget {
  std::optional<std::size_t> remaining;
};

template <class T> struct TestAllocator {
  using value_type = T;
  AllocationBudget *budget;
  explicit TestAllocator(AllocationBudget &allocation_budget)
      : budget(&allocation_budget) {}
  template <class U>
  TestAllocator(const TestAllocator<U> &other) : budget(other.budget) {}
  template <class U>
  bool operator==(const TestAllocator<U> &other) const {
    return budget == other.budget;
  }
  T *allocate(std::size_t count) {
    if (auto &remaining = budget->remaining; remaining) {
      if (*remaining == 0)
        throw std::bad_alloc();
      --*remaining;
    }
    return std::allocator<T>{}.allocate(count);
  }
  void deallocate(T *pointer, std::size_t count) noexcept {
    std::allocator<T>{}.deallocate(pointer, count);
  }
};

void failed_staging_leaves_makers_and_index_intact() {
  // Opening a new level needs a level node, a resting order node, and an
  // index node. Trigger failure at each allocation before the match commits.
  AllocationBudget budget;
  Matcher<TestAllocator<std::byte>> book({1}, 2, {},
                                         TestAllocator<std::byte>{budget});
  auto plan = book.make_plan();
  const auto maker = execute(book, plan, make_order(0, Side::Sell, 100, 1));
  auto place = make_order(1, Side::Buy, 100, 2);
  book.prepare(place, plan);
  CHECK(plan.reservations_released == 1 && plan.needs_reservation);

  for (std::size_t allocations = 0; allocations != 3; ++allocations) {
    budget.remaining = allocations;
    bool failed = false;
    try {
      book.commit(plan);
    } catch (const std::bad_alloc &) {
      failed = true;
    }
    budget.remaining.reset();
    CHECK(failed);
    const auto orders = book.snapshot();
    CHECK(orders.size() == 1 && orders[0].order_id == maker);
    CHECK(orders[0].remaining == 1 && orders[0].executed == 0);
  }
  CHECK(execute(book, plan, place) == maker + 1);
  CHECK(book.snapshot()[0].order_id == maker + 1);
}
} // namespace

int main() {
  try {
    rejecting_an_oversized_sweep_avoids_partial_execution();
    a_cross_reuses_capacity_from_removed_makers();
    a_reprice_uses_the_origin_reservation();
    failed_staging_leaves_makers_and_index_intact();
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "matcher_test: " << error.what() << '\n';
    return 1;
  }
}
