// Dedicated evidence builds only. Capture malloc calls from proven machine
// return addresses in the fresh-backing branch of Simplx's Event allocator.
#include <sched.h>
#include <time.h>
#include <unistd.h>

#include <array>
#include <atomic>
#include <charconv>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <string_view>

namespace {

struct Record {
  std::uint64_t monotonic_ns;
  std::size_t bytes;
  std::uintptr_t return_address;
  int cpu;
  int tid;
};

std::array<Record, 16384> rows;
std::array<std::uintptr_t, 32> sites;
std::atomic<std::size_t> observed{};
std::size_t site_count{};
bool valid{};
const char *output{};

__attribute__((constructor)) void configure() noexcept {
  output = std::getenv("CT_EVENT_PAGE_LOG");
  const auto *value = std::getenv("CT_EVENT_PAGE_SITES");
  if (!value || !output)
    return;
  std::string_view list(value);
  while (!list.empty() && site_count < sites.size()) {
    const auto delimiter = list.find(',');
    const auto token = list.substr(0, delimiter);
    const auto [end, error] = std::from_chars(token.data(), token.data() + token.size(),
                                             sites[site_count], 16);
    if (error != std::errc{} || end != token.data() + token.size() || !sites[site_count])
      return;
    ++site_count;
    if (delimiter == std::string_view::npos) {
      valid = true;
      return;
    }
    list.remove_prefix(delimiter + 1);
  }
}

__attribute__((destructor)) void report() noexcept {
  if (!output)
    return;
  auto *file = std::fopen(output, "w");
  if (!file) {
    std::perror("Event page trace");
    return;
  }
  const auto count = observed.load(std::memory_order_relaxed);
  const auto stored = count <= rows.size() ? count : rows.size();
  std::fprintf(file, "{\"configured\":%s,\"sites\":[", valid ? "true" : "false");
  for (std::size_t index = 0; index < site_count; ++index)
    std::fprintf(file, "%s\"%jx\"", index ? "," : "",
                 static_cast<std::uintmax_t>(sites[index]));
  std::fprintf(file, "],\"lost_samples\":%zu,\"allocations\":[", count - stored);
  for (std::size_t index = 0; index < stored; ++index) {
    const auto &row = rows[index];
    std::fprintf(file, "%s{\"monotonic_ns\":%ju,\"bytes\":%zu,\"return_address\":\"%jx\","
                 "\"cpu\":%d,\"thread_id\":%d}", index ? "," : "",
                 static_cast<std::uintmax_t>(row.monotonic_ns), row.bytes,
                 static_cast<std::uintmax_t>(row.return_address), row.cpu, row.tid);
  }
  std::fputs("]}\n", file);
  if (std::fclose(file) != 0)
    std::perror("Event page trace");
}

} // namespace

extern "C" void *__real_malloc(std::size_t bytes);

extern "C" void *__wrap_malloc(std::size_t bytes) {
  auto *result = __real_malloc(bytes);
  if (!result || !valid)
    return result;
  const auto address = reinterpret_cast<std::uintptr_t>(
      __builtin_extract_return_addr(__builtin_return_address(0)));
  for (std::size_t site = 0; site < site_count; ++site) {
    if (address != sites[site])
      continue;
    const auto index = observed.fetch_add(1, std::memory_order_relaxed);
    if (index < rows.size()) {
      timespec now{};
      ::clock_gettime(CLOCK_MONOTONIC, &now);
      rows[index] = {static_cast<std::uint64_t>(now.tv_sec) * 1'000'000'000 +
                     static_cast<std::uint64_t>(now.tv_nsec), bytes, address,
                     ::sched_getcpu(), ::gettid()};
    }
    break;
  }
  return result;
}
