#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace compute_trade {

struct ExchangeConfig {
    unsigned core{}; // Linux logical CPU ID, within the process's allowed affinity mask.
    std::string bind_address{"127.0.0.1"};
    std::uint16_t port{9000}; // Zero selects an ephemeral port (useful for tests).
    std::vector<std::uint32_t> instruments{1, 2};
};

struct Placement {
    unsigned core{};
    int oeg_cpu{-1};
    int matching_cpu{-1};
};

// Starts one pinned Simplx event loop containing the OEG and matching actors.
// Destruction stops acceptance and closes sessions; callers should drain acknowledgements first.
class Exchange {
public:
    explicit Exchange(ExchangeConfig config);
    ~Exchange();
    Exchange(const Exchange&) = delete;
    Exchange& operator=(const Exchange&) = delete;
    std::uint16_t port() const noexcept;
    Placement placement() const noexcept;
    bool failed() const noexcept;
    std::string error() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

unsigned first_allowed_core();
void validate_core(unsigned core);

} // namespace compute_trade
