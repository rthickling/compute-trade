#include "compute_trade/exchange.hpp"

#include <charconv>
#include <chrono>
#include <csignal>
#include <iostream>
#include <stdexcept>
#include <string_view>
#include <thread>

namespace {
volatile std::sig_atomic_t stopping = 0;
void stop(int) { stopping = 1; }
unsigned number(std::string_view value, unsigned maximum) {
    unsigned result{};
    const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
    if (error != std::errc{} || end != value.data() + value.size() || result > maximum)
        throw std::invalid_argument("invalid numeric option");
    return result;
}
}

int main(int argc, char** argv) {
    try {
        compute_trade::ExchangeConfig config;
        config.core = compute_trade::first_allowed_core();
        for (int i = 1; i < argc; ++i) {
            const std::string_view option(argv[i]);
            if (option == "--help") {
                std::cout << "Usage: ct_exchange [--core CPU] [--bind IPv4] [--port PORT]\n"
                             "Default: first allowed CPU, 127.0.0.1:9000; instruments 1 and 2.\n";
                return 0;
            }
            if (i + 1 == argc) throw std::invalid_argument("missing option value");
            const std::string_view value(argv[++i]);
            if (option == "--core") config.core = number(value, 255);
            else if (option == "--port") config.port = static_cast<std::uint16_t>(number(value, 65535));
            else if (option == "--bind") config.bind_address = value;
            else throw std::invalid_argument("unknown option: " + std::string(option));
        }
        std::signal(SIGINT, stop);
        std::signal(SIGTERM, stop);
        compute_trade::Exchange exchange(config);
        const auto placement = exchange.placement();
        std::cout << "READY port=" << exchange.port() << " core=" << placement.core
                  << " oeg_cpu=" << placement.oeg_cpu << " matching_cpu=" << placement.matching_cpu
                  << std::endl;
        while (!stopping && !exchange.failed()) std::this_thread::sleep_for(std::chrono::milliseconds(20));
        if (exchange.failed()) throw std::runtime_error(exchange.error());
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "ct_exchange: " << error.what() << '\n';
        return 1;
    }
}
