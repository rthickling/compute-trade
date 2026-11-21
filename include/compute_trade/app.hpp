#pragma once

#include <csignal>
#include <cstdint>
#include <string>

namespace compute_trade {

struct AppOptions {
  std::string config;
  std::string control_path;
  std::string bind_address{"127.0.0.1"};
  std::uint16_t port{9000};
  std::uint16_t market_port{9001};
  int core{-1};
  int publisher_core{-1};
  int blue_core{-1};
  bool redzone{};
};

int run_app(const AppOptions &, const volatile std::sig_atomic_t &stopping);

} // namespace compute_trade
