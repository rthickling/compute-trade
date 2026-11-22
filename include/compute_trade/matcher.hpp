#pragma once

#include "compute_trade/types.hpp"

#include <algorithm>
#include <array>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <iterator>
#include <limits>
#include <list>
#include <map>
#include <memory>
#include <stdexcept>
#include <string_view>
#include <unordered_map>
#include <utility>
#include <vector>

namespace compute_trade {

enum class MatchKind { New, Cancel, Replace, Disconnect };
enum class ChangeKind { Add, Modify, Remove };

struct MatchLimits {
  std::size_t live_orders_total = 4096;
  std::size_t live_orders_per_session = 1024;
  std::size_t active_prices = 2048;
  std::size_t matching_max_fills = 64;
};

struct MatchCommand {
  MatchKind kind{};
  std::size_t session{};
  std::array<char, 8> firm{};
  std::uint32_t instrument{};
  Side side{};
  std::int64_t price{};
  std::uint64_t quantity{};
  std::uint64_t order_id{};
  std::uint8_t order_type = 2;
};

struct BookOrder {
  std::uint64_t order_id{};
  std::uint64_t priority{};
  std::uint32_t instrument{};
  Side side{};
  std::int64_t price{};
  std::uint64_t remaining{};
  std::uint64_t executed{};
  std::array<char, 8> firm{};
  std::size_t session{};
};

struct BookChange {
  ChangeKind kind{};
  BookOrder order;
};

// Quantity and both order states immediately after this execution. The trade
// price is maker.price. Earlier fills are not rewritten by later fills.
struct MatchFill {
  BookOrder maker;
  BookOrder taker;
  std::uint64_t quantity{};
};

struct MatchPlan {
  MatchKind kind{};
  std::string_view rejection;
  BookOrder result;
  std::vector<BookChange> changes;
  std::vector<MatchFill> fills;
  bool needs_reservation{};
  std::size_t reservations_released{};
  std::size_t maker_visits{};
  std::size_t price_visits{};
};

// Single-owner matching state. The gateway authorises the session and supplies
// commands in sequence. One plan belongs to the current book state and must be
// committed before the next mutation. Capacity checks and exact output counts
// are available before applying any mutation.
template <class Allocator = std::allocator<std::byte>> class Matcher {
  template <class T>
  using Rebind =
      typename std::allocator_traits<Allocator>::template rebind_alloc<T>;
  using OrderAllocator = Rebind<BookOrder>;
  using Queue = std::list<BookOrder, OrderAllocator>;
  using OrderPosition = typename Queue::iterator;

  struct PriceKey {
    std::uint32_t instrument;
    Side side;
    std::int64_t price;
    bool operator==(const PriceKey &) const = default;
  };

  struct PricePriority {
    bool operator()(const PriceKey &lhs, const PriceKey &rhs) const noexcept {
      if (lhs.instrument != rhs.instrument)
        return lhs.instrument < rhs.instrument;
      if (lhs.side != rhs.side)
        return lhs.side < rhs.side;
      return lhs.side == Side::Buy ? lhs.price > rhs.price
                                   : lhs.price < rhs.price;
    }
  };

  using LevelAllocator = Rebind<std::pair<const PriceKey, Queue>>;
  using Levels = std::map<PriceKey, Queue, PricePriority, LevelAllocator>;
  using LevelPosition = typename Levels::iterator;

  struct LiveOrder {
    OrderPosition position;
    LevelPosition level;
    LiveOrder *previous{};
    LiveOrder *next{};
  };

  // unordered_map rehash invalidates iterators but preserves element pointers.
  // New IDs are appended in increasing order. An order stays linked to its
  // original session through price or priority changes and unlinks on removal.
  struct SessionOrders {
    LiveOrder *head{};
    LiveOrder *tail{};
    std::size_t count{};

    void append(LiveOrder &order) noexcept {
      order.previous = tail;
      if (tail)
        tail->next = &order;
      else
        head = &order;
      tail = &order;
      ++count;
    }

    void unlink(const LiveOrder &order) noexcept {
      if (order.previous)
        order.previous->next = order.next;
      else
        head = order.next;
      if (order.next)
        order.next->previous = order.previous;
      else
        tail = order.previous;
      --count;
    }
  };

  using IndexAllocator = Rebind<std::pair<const std::uint64_t, LiveOrder>>;
  using Index =
      std::unordered_map<std::uint64_t, LiveOrder, std::hash<std::uint64_t>,
                         std::equal_to<std::uint64_t>, IndexAllocator>;

  static constexpr auto null_order_id =
      std::numeric_limits<std::uint64_t>::max();

public:
  Matcher(std::vector<std::uint32_t> instruments, std::size_t session_count,
          MatchLimits limits, const Allocator &allocator = {})
      : allocator_(allocator), instruments_(std::move(instruments)),
        session_orders_(session_count), limits_(limits),
        levels_(PricePriority{}, LevelAllocator{allocator}),
        by_id_(0, std::hash<std::uint64_t>{}, std::equal_to<std::uint64_t>{},
               IndexAllocator{allocator}) {
    if (limits.matching_max_fills == std::numeric_limits<std::size_t>::max() ||
        limits.live_orders_total == std::numeric_limits<std::size_t>::max())
      throw std::invalid_argument("matcher capacity is too large");
    std::sort(instruments_.begin(), instruments_.end());
    // A pending insertion can stage its node before releasing filled makers.
    by_id_.reserve(limits.live_orders_total + 1);
  }

  Matcher(const Matcher &) = delete;
  Matcher &operator=(const Matcher &) = delete;

  [[nodiscard]] MatchPlan make_plan() const {
    MatchPlan plan;
    plan.changes.reserve(std::max(
        limits_.matching_max_fills + 1,
        std::min(limits_.live_orders_total, limits_.live_orders_per_session)));
    plan.fills.reserve(limits_.matching_max_fills);
    return plan;
  }

  void prepare(const MatchCommand &request, MatchPlan &plan) const {
    plan.kind = request.kind;
    plan.rejection = {};
    plan.result = {};
    plan.changes.clear();
    plan.fills.clear();
    plan.needs_reservation = false;
    plan.reservations_released = 0;
    plan.maker_visits = 0;
    plan.price_visits = 0;

    if (request.session >= session_orders_.size())
      return reject(plan, "unknown_session");
    if (request.kind == MatchKind::Disconnect)
      return prepare_disconnect(request.session, plan);

    if (!std::binary_search(instruments_.begin(), instruments_.end(),
                            request.instrument))
      return reject(plan, "unknown_instrument");
    if (request.side != Side::Buy && request.side != Side::Sell)
      return reject(plan, "incompatible_order");

    if (request.kind == MatchKind::New)
      return prepare_new(request, plan);

    const auto target = by_id_.find(request.order_id);
    if (target == by_id_.end())
      return reject(plan, "unknown_order");
    const LiveOrder &entry = target->second;
    const BookOrder &original = *entry.position;
    if (original.firm != request.firm)
      return reject(plan, "wrong_owner");
    if (original.instrument != request.instrument ||
        original.side != request.side || request.order_type != 2)
      return reject(plan, "incompatible_order");

    if (request.kind == MatchKind::Cancel) {
      plan.result = original;
      append_removal(plan, original);
      return;
    }
    prepare_replace(request, entry, plan);
  }

  void commit(const MatchPlan &plan) {
    if (!plan.rejection.empty())
      return;

    const bool replacing = plan.kind == MatchKind::Replace;
    if (plan.kind == MatchKind::New) {
      // Stage all potentially throwing allocations before adjusting makers.
      if (plan.result.remaining != 0)
        insert_new(plan.result);
      ++next_id_;
      ++next_priority_;
    } else if (replacing && plan.result.remaining != 0) {
      LiveOrder &entry = by_id_.at(plan.result.order_id);
      if (entry.position->priority != plan.result.priority) {
        move_to_tail(entry, plan.result);
        ++next_priority_;
      } else {
        *entry.position = plan.result;
      }
    } else if (replacing) {
      ++next_priority_;
    }

    for (const BookChange &change : plan.changes) {
      if (change.kind == ChangeKind::Remove) {
        remove_order(change.order.order_id);
      } else if (change.kind == ChangeKind::Modify &&
                 (!replacing ||
                  change.order.order_id != plan.result.order_id)) {
        *by_id_.at(change.order.order_id).position = change.order;
      }
    }
  }

  [[nodiscard]] std::vector<BookOrder> snapshot() const {
    std::vector<BookOrder> orders;
    orders.reserve(by_id_.size());
    for (const auto &[key, queue] : levels_)
      orders.insert(orders.end(), queue.begin(), queue.end());
    return orders;
  }

private:
  static PriceKey key(const BookOrder &order) noexcept {
    return {order.instrument, order.side, order.price};
  }

  static void reject(MatchPlan &plan, std::string_view reason) noexcept {
    plan.rejection = reason;
    plan.result = {};
    plan.changes.clear();
    plan.fills.clear();
    plan.needs_reservation = false;
    plan.reservations_released = 0;
  }

  static void append_removal(MatchPlan &plan, BookOrder removed) {
    removed.remaining = 0;
    plan.changes.push_back({ChangeKind::Remove, removed});
    ++plan.reservations_released;
  }

  void prepare_disconnect(std::size_t session, MatchPlan &plan) const {
    for (const LiveOrder *order = session_orders_[session].head; order;
         order = order->next)
      append_removal(plan, *order->position);
  }

  void prepare_new(const MatchCommand &request, MatchPlan &plan) const {
    if (request.order_type != 2 || request.price <= 0 ||
        request.quantity == 0 || request.quantity == null_order_id)
      return reject(plan, "invalid_order");
    if (next_id_ == null_order_id || next_priority_ == null_order_id)
      return reject(plan, "identifier_capacity");

    plan.result = {next_id_,
                   next_priority_,
                   request.instrument,
                   request.side,
                   request.price,
                   request.quantity,
                   0,
                   request.firm,
                   request.session};
    const std::size_t vacated_prices = plan_sweep(plan);
    if (!plan.rejection.empty() || plan.result.remaining == 0)
      return;

    std::size_t session_live = session_orders_[request.session].count;
    for (const auto &change : plan.changes)
      if (change.kind == ChangeKind::Remove &&
          change.order.session == request.session)
        --session_live;
    if (session_live >= limits_.live_orders_per_session)
      return reject(plan, "session_live_order_capacity");
    if (by_id_.size() - plan.reservations_released >= limits_.live_orders_total)
      return reject(plan, "live_order_capacity");
    if (!levels_.contains(key(plan.result)) &&
        levels_.size() - vacated_prices >= limits_.active_prices)
      return reject(plan, "active_price_capacity");

    plan.needs_reservation = true;
    plan.changes.push_back({ChangeKind::Add, plan.result});
  }

  void prepare_replace(const MatchCommand &request, const LiveOrder &entry,
                       MatchPlan &plan) const {
    const BookOrder &original = *entry.position;
    if (request.price <= 0 || request.quantity <= original.executed ||
        request.quantity == null_order_id)
      return reject(plan, "invalid_amendment");

    plan.result = original;
    plan.result.price = request.price;
    plan.result.remaining = request.quantity - original.executed;
    const bool repriced = request.price != original.price;
    if (!repriced && plan.result.remaining <= original.remaining) {
      plan.changes.push_back({ChangeKind::Modify, plan.result});
      return;
    }

    if (next_priority_ == null_order_id)
      return reject(plan, "identifier_capacity");
    plan.result.priority = next_priority_;
    std::size_t vacated_prices = plan_sweep(plan);
    if (!plan.rejection.empty())
      return;

    if (plan.result.remaining == 0) {
      // Publish the old order's removal even when its attempted new price
      // differed. The private fill already carries the execution price.
      append_removal(plan, original);
    } else {
      if (repriced && !levels_.contains(key(plan.result))) {
        if (entry.level->second.size() == 1)
          ++vacated_prices;
        if (levels_.size() - vacated_prices >= limits_.active_prices)
          return reject(plan, "active_price_capacity");
      }
      plan.changes.push_back({ChangeKind::Modify, plan.result});
    }
  }

  std::size_t plan_sweep(MatchPlan &plan) const {
    BookOrder &incoming = plan.result;
    const Side opposite = incoming.side == Side::Buy ? Side::Sell : Side::Buy;
    const auto first = levels_.lower_bound(
        {incoming.instrument, opposite,
         opposite == Side::Buy ? std::numeric_limits<std::int64_t>::max() : 1});
    std::size_t vacated_prices = 0;
    for (auto level = first; level != levels_.end() && incoming.remaining != 0;
         ++level) {
      const auto &maker_key = level->first;
      if (maker_key.instrument != incoming.instrument ||
          maker_key.side != opposite ||
          (incoming.side == Side::Buy ? incoming.price < maker_key.price
                                      : incoming.price > maker_key.price))
        break;
      ++plan.price_visits;
      const auto &queue = level->second;
      for (auto order = queue.begin();
           order != queue.end() && incoming.remaining != 0; ++order) {
        if (plan.fills.size() == limits_.matching_max_fills) {
          reject(plan, "sweep_capacity");
          return 0;
        }
        ++plan.maker_visits;
        BookOrder maker = *order;
        const auto quantity = std::min(maker.remaining, incoming.remaining);
        maker.remaining -= quantity;
        maker.executed += quantity;
        incoming.remaining -= quantity;
        incoming.executed += quantity;
        plan.fills.push_back({maker, incoming, quantity});
        if (maker.remaining == 0) {
          append_removal(plan, maker);
          if (std::next(order) == queue.end())
            ++vacated_prices;
        } else {
          plan.changes.push_back({ChangeKind::Modify, maker});
        }
      }
    }
    return vacated_prices;
  }

  void insert_new(const BookOrder &order) {
    const auto [level, created] =
        levels_.try_emplace(key(order), OrderAllocator{allocator_});
    try {
      auto &queue = level->second;
      queue.push_back(order);
      try {
        const auto [entry, inserted] = by_id_.try_emplace(
            order.order_id, LiveOrder{std::prev(queue.end()), level});
        if (!inserted)
          throw std::logic_error("order ID reused");
        session_orders_[order.session].append(entry->second);
      } catch (...) {
        queue.pop_back();
        throw;
      }
    } catch (...) {
      if (created)
        levels_.erase(level);
      throw;
    }
  }

  void move_to_tail(LiveOrder &entry, const BookOrder &replacement) {
    const auto old = entry.level;
    const auto target =
        levels_.try_emplace(key(replacement), OrderAllocator{allocator_}).first;
    auto &queue = target->second;
    queue.splice(queue.end(), old->second, entry.position);
    *entry.position = replacement;
    entry.level = target;
    if (old->second.empty())
      levels_.erase(old);
  }

  void remove_order(std::uint64_t id) {
    const auto item = by_id_.find(id);
    assert(item != by_id_.end());
    const LiveOrder &order = item->second;
    const auto level = order.level;
    session_orders_[order.position->session].unlink(order);
    level->second.erase(order.position);
    by_id_.erase(item);
    if (level->second.empty())
      levels_.erase(level);
  }

  Allocator allocator_;
  std::vector<std::uint32_t> instruments_;
  std::vector<SessionOrders> session_orders_;
  MatchLimits limits_;
  Levels levels_;
  Index by_id_;
  std::uint64_t next_id_{1};
  std::uint64_t next_priority_{1};
};

} // namespace compute_trade
