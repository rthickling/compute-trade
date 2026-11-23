#pragma once

#include <simplx.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>

namespace compute_trade {

struct BookChange;

struct MarketUpdate {
  enum class Action { add, modify, remove };
  Action action;
  std::uint32_t instrument;
  std::uint64_t order_id;
  std::uint8_t side;
  std::int64_t price;
  std::uint64_t remaining;
  std::uint64_t priority;
};

struct NoticeRecord {
  std::size_t slot;
  std::uint32_t sequence;
  std::uint64_t order_id;
  std::uint32_t instrument;
  std::uint32_t access;
  std::uint16_t partition;
  std::array<char, 8> firm;
  std::uint64_t book_in_time{};
};

struct WorkerLocation {
  std::string_view role;
  int cpu;
  int thread_id;
};

struct WorkerStatistics {
  std::uint64_t commands_completed;
  std::uint64_t market_updates_sent;
  std::uint64_t notices_copied;
  // Publisher callbacks observed while diagnostics are enabled. Ordinary runs
  // leave this at zero; the counter itself is not incremented on that path.
  std::uint64_t publisher_callbacks;
};

struct WorkersConfig {
  std::string bind_address{"127.0.0.1"};
  std::uint16_t market_port{};
  std::size_t market_capacity{4096};
  std::size_t notice_handoff_slots{8};
  std::size_t notice_batch_size{16};
  std::size_t notice_capacity{4096};
  // The callback can run on either worker. It is empty in ordinary runs.
  std::function<void(std::string_view, std::string)> diagnostic;
};

class Workers {
public:
  explicit Workers(WorkersConfig config);
  ~Workers();
  Workers(const Workers &) = delete;
  Workers &operator=(const Workers &) = delete;

  void add_actors(tredzone::Engine::StartSequence &sequence,
                  tredzone::Actor::CoreId publisher_cpu,
                  tredzone::Actor::CoreId blue_cpu);
  std::uint16_t market_port() const noexcept;
  bool market_failed() const noexcept;
  std::array<WorkerLocation, 2> placement() const noexcept;
  WorkerStatistics statistics() const noexcept;

  // The matcher is the single producer for the publication and notice rings.
  std::size_t market_available() const noexcept;
  void publish(tredzone::Actor &sender, std::span<const BookChange> changes,
               std::uint64_t command_sequence);

  bool offer_notices(tredzone::Actor &sender, std::uint64_t batch_id,
                     std::span<const NoticeRecord> records);
  std::span<const std::uint64_t> collect_notice_batches() noexcept;
  std::optional<NoticeRecord> retained_notice(std::size_t slot) const noexcept;
  void release_notice(std::size_t slot) noexcept;
  void set_notice_copy(bool enabled) noexcept;
  // Called only by the matcher. BlueZone samples the cumulative count.
  void record_command() noexcept;

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

} // namespace compute_trade
