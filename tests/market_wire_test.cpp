#include "market_wire.hpp"

#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {
using compute_trade::MarketUpdate;

#define CHECK(condition)                                                        \
  do {                                                                          \
    if (!(condition))                                                           \
      throw std::runtime_error("line " + std::to_string(__LINE__) + ": " #condition); \
  } while (false)

template <class T>
T load(const std::array<std::byte, 64> &bytes, std::size_t offset) {
  T value{};
  std::memcpy(&value, bytes.data() + offset, sizeof(T));
  return value;
}

void update_frame_matches_the_published_layout() {
  static_assert(sizeof(compute_trade::market_wire::UpdateFrame) == 64);
  static_assert(alignof(compute_trade::market_wire::UpdateFrame) == 64);
  static_assert(static_cast<std::uint8_t>(MarketUpdate::Action::add) == 0);
  static_assert(static_cast<std::uint8_t>(MarketUpdate::Action::modify) == 1);
  static_assert(static_cast<std::uint8_t>(MarketUpdate::Action::remove) == 2);

  compute_trade::market_wire::UpdateFrame frame{};
  const MarketUpdate update{
      .action = MarketUpdate::Action::modify, .instrument = 7,
      .order_id = 0x0102030405060708, .side = 2, .price = -5,
      .remaining = 9, .priority = 11};
  compute_trade::market_wire::encode(frame, update, 0x1112131415161718, 3);

  CHECK(load<std::uint16_t>(frame.bytes, 0) == 64);
  CHECK(load<std::uint16_t>(frame.bytes, 2) == 54);
  CHECK(load<std::uint16_t>(frame.bytes, 4) == 3);
  CHECK(load<std::uint16_t>(frame.bytes, 6) == 2);
  CHECK(load<std::uint16_t>(frame.bytes, 8) == 1);
  CHECK(load<std::uint64_t>(frame.bytes, 10) == 3);
  CHECK(load<std::uint64_t>(frame.bytes, 18) == 0x1112131415161718);
  CHECK(load<std::uint8_t>(frame.bytes, 26) == 1);
  CHECK(load<std::uint32_t>(frame.bytes, 27) == 7);
  CHECK(load<std::uint64_t>(frame.bytes, 31) == 0x0102030405060708);
  CHECK(load<std::uint8_t>(frame.bytes, 39) == 2);
  CHECK(load<std::int64_t>(frame.bytes, 40) == -5);
  CHECK(load<std::uint64_t>(frame.bytes, 48) == 9);
  CHECK(load<std::uint64_t>(frame.bytes, 56) == 11);
}
} // namespace

int main() {
  try {
    update_frame_matches_the_published_layout();
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "market_wire_test: " << error.what() << '\n';
    return 1;
  }
}
