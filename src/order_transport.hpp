#pragma once

#include "compute_trade/optiq.hpp"

#include <simplx.h>

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <deque>
#include <functional>
#include <stdexcept>
#include <string_view>
#include <vector>

namespace compute_trade {

struct AppOptions;
struct Config;
class Diagnostics;

namespace order_transport {

inline constexpr std::size_t inbound_frames = 4;

template <class T> class SpscRing {
  static_assert(std::atomic<std::size_t>::is_always_lock_free);

  std::vector<T> storage_;
  alignas(64) std::atomic<std::size_t> read_{};
  alignas(64) std::atomic<std::size_t> write_{};

  std::size_t next(std::size_t index) const noexcept {
    ++index;
    return index == storage_.size() ? 0 : index;
  }

public:
  explicit SpscRing(std::size_t capacity) : storage_(capacity + 1) {}

  // The producer owns write_, and the consumer owns read_. Each may run
  // concurrently with its peer. A front pointer remains valid until pop().
  std::size_t available() const noexcept {
    const auto write = write_.load(std::memory_order_relaxed);
    const auto read = read_.load(std::memory_order_acquire);
    return read > write ? read - write - 1 : storage_.size() - write + read - 1;
  }

  void push(const T &value) {
    const auto write = write_.load(std::memory_order_relaxed);
    const auto after = next(write);
    if (after == read_.load(std::memory_order_acquire))
      throw std::logic_error("order transport ring overflow");
    storage_[write] = value;
    write_.store(after, std::memory_order_release);
  }

  const T *front() const noexcept {
    const auto read = read_.load(std::memory_order_relaxed);
    return read == write_.load(std::memory_order_acquire) ? nullptr
                                                          : &storage_[read];
  }

  bool empty() const noexcept {
    return read_.load(std::memory_order_acquire) ==
           write_.load(std::memory_order_acquire);
  }

  void pop() noexcept {
    read_.store(next(read_.load(std::memory_order_relaxed)),
                std::memory_order_release);
  }

  // Both participants have released the slot before this is called.
  void reset() noexcept {
    read_.store(0, std::memory_order_relaxed);
    write_.store(0, std::memory_order_relaxed);
  }
};

enum class Phase { free, open, closed, retired };

struct Reply {
  // A zero-size frame requests encoding of the connection's staged session message.
  optiq::Frame frame{};
  std::uint32_t sequence{};
  bool end_connection{};

  Reply &operator=(const Reply &other) noexcept {
    if (this != &other) {
      std::memcpy(frame.bytes.data(), other.frame.bytes.data(), other.frame.size);
      frame.size = other.frame.size;
      sequence = other.sequence;
      end_connection = other.end_connection;
    }
    return *this;
  }
};

struct Slot {
  explicit Slot(std::size_t reply_slots)
      : inbound(inbound_frames), outbound(reply_slots) {}

  SpscRing<optiq::Frame> inbound;
  SpscRing<Reply> outbound;
  // Only one deferred session reply can be outstanding. The outbound ring publishes
  // this message before BlueZone uses it and releases it after the final byte.
  optiq::Message session_message{};
  // Blue publishes open and closed. Red publishes retired once it has
  // detached the logical connection and finished using both rings.
  std::atomic<Phase> phase{Phase::free};
  std::atomic<bool> close_requested{false};
  std::atomic<std::size_t> session{SIZE_MAX};
  std::atomic<bool> allow_intake{true};
  std::uint64_t opened_ns{};
};

struct SendGate {
  std::atomic<bool> allow{true};
  std::atomic<std::size_t> remaining{SIZE_MAX};
};

struct Transport {
  explicit Transport(const Config &config);

  const Config &config;
  std::deque<Slot> slots;
  std::vector<SendGate> send_gates;
  std::atomic<std::uint64_t> accepted_connections{};
  std::atomic<bool> intake_paused{false};
  std::atomic<std::uint64_t> monotonic_ns{};
  std::atomic<std::uint64_t> epoch_ns{};
  std::atomic<int> tid{-1};
  std::atomic<int> cpu{-1};
  std::uint16_t port{};
};

void add_actor(tredzone::Engine::StartSequence &start,
               tredzone::Actor::CoreId cpu, const AppOptions &options,
               Transport &transport, Diagnostics &diagnostics,
               std::function<void(std::string_view)> fail);

} // namespace order_transport
} // namespace compute_trade
