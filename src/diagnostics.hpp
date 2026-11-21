#pragma once

#include "json.hpp"

#include <cstdint>
#include <deque>
#include <mutex>
#include <optional>

namespace compute_trade {

class Diagnostics {
  const bool enabled_;
  mutable std::mutex mutex_;
  std::deque<Json> history_;
  std::uint64_t next_index_{};
  static constexpr std::size_t capacity = 4096;

public:
  explicit Diagnostics(bool enabled) : enabled_(enabled) {}

  bool enabled() const { return enabled_; }

  void record(std::string_view event, Json row) {
    if (!enabled_)
      return;
    std::lock_guard lock(mutex_);
    row.set("event", event).set("event_index", ++next_index_);
    if (history_.size() >= capacity)
      history_.pop_front();
    history_.push_back(std::move(row));
  }

  std::optional<std::string> find(std::string_view event, const Json &match) const {
    std::lock_guard lock(mutex_);
    for (const auto &row : history_) {
      if (row.get("event").string() == event && row.includes(match))
        return row.dump();
    }
    return {};
  }
};

} // namespace compute_trade
