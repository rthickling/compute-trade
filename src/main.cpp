#include "compute_trade/app.hpp"

#include <charconv>
#include <csignal>
#include <iostream>
#include <stdexcept>
#include <string_view>

namespace {
volatile std::sig_atomic_t stopping = 0;
void stop(int) { stopping = 1; }
unsigned number(std::string_view value, unsigned maximum) {
  unsigned result{};
  const auto [end, error] =
      std::from_chars(value.data(), value.data() + value.size(), result);
  if (error != std::errc{} || end != value.data() + value.size() ||
      result > maximum)
    throw std::invalid_argument("invalid numeric option");
  return result;
}
} // namespace

int main(int argc, char **argv) {
  try {
    compute_trade::AppOptions app;
    for (int i = 1; i < argc; ++i) {
      const std::string_view option(argv[i]);
      if (option == "--help") {
        std::cout
            << "Usage: ct_exchange --config FILE [--bind IPv4] [--port PORT]\n"
               "                   [--market-port PORT] [--core CPU]\n"
               "                   [--publisher-core CPU] [--blue-core CPU] [--redzone]\n"
               "Default listeners: 127.0.0.1:9000 for order entry, 127.0.0.1:9001 for market data.\n"
               "Startup placement: --placement-file FILE\n"
               "Ordinary testing only: --acceptance-control PATH\n";
        return 0;
      }
      if (option == "--redzone") {
        app.redzone = true;
        continue;
      }
      if (i + 1 == argc)
        throw std::invalid_argument("missing option value");
      const std::string_view value(argv[++i]);
      if (option == "--core")
        app.core = static_cast<int>(number(value, 255));
      else if (option == "--port")
        app.port = static_cast<std::uint16_t>(number(value, 65535));
      else if (option == "--bind")
        app.bind_address = value;
      else if (option == "--config")
        app.config = value;
      else if (option == "--market-port")
        app.market_port = static_cast<std::uint16_t>(number(value, 65535));
      else if (option == "--publisher-core")
        app.publisher_core = static_cast<int>(number(value, 255));
      else if (option == "--blue-core")
        app.blue_core = static_cast<int>(number(value, 255));
      else if (option == "--acceptance-control")
        app.control_path = value;
      else if (option == "--placement-file")
        app.placement_path = value;
      else
        throw std::invalid_argument("unknown option: " + std::string(option));
    }
    if (app.config.empty())
      throw std::invalid_argument("--config FILE is required");
    std::signal(SIGINT, stop);
    std::signal(SIGTERM, stop);
    return compute_trade::run_app(app, stopping);
  } catch (const std::exception &error) {
    std::cerr << "ct_exchange: " << error.what() << '\n';
    return 1;
  }
}
