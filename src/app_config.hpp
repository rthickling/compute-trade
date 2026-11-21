#pragma once

#include "compute_trade/app.hpp"
#include "compute_trade/exchange.hpp"
#include "compute_trade/matcher.hpp"
#include "compute_trade/optiq.hpp"
#include "json.hpp"

#include <sched.h>

#include <algorithm>
#include <charconv>
#include <fstream>
#include <map>
#include <set>
#include <tuple>

namespace compute_trade {

struct SessionKey {
  std::uint32_t access;
  std::uint16_t partition;
  auto operator<=>(const SessionKey &) const = default;
};

struct SessionRule {
  SessionKey key;
  std::vector<std::array<char, 8>> firms;
};

struct Limits {
  MatchLimits book{1024, 64, 512, 16};
  std::size_t queue{128};
  std::size_t reply_slots{64};
  std::size_t ordinary_replay{128};
  std::size_t notices{2048};
  std::size_t notice_handoff{4};
  std::size_t notice_batch{32};
  std::size_t publication{1024};
};

struct Config {
  Limits limits;
  std::vector<std::uint32_t> instruments;
  std::vector<SessionRule> sessions;
};

inline std::string read_file(const std::string &path) {
  std::ifstream file(path, std::ios::binary);
  if (!file)
    throw std::invalid_argument("cannot read " + path);
  std::string result(65537, '\0');
  file.read(result.data(), static_cast<std::streamsize>(result.size()));
  if (file.bad() || file.gcount() >= static_cast<std::streamsize>(result.size()))
    throw std::invalid_argument("file exceeds 64 KiB or cannot be read: " + path);
  result.resize(static_cast<std::size_t>(file.gcount()));
  return result;
}

inline std::size_t bounded(const Json &value, std::size_t maximum, const char *name) {
  const auto number = value.number();
  if (number == 0 || number > maximum)
    throw std::invalid_argument(std::string("invalid ") + name);
  return static_cast<std::size_t>(number);
}

inline Config load_config(const std::string &path) {
  const auto root = Json::parse(read_file(path));
  Config result;
  auto instruments = root.get("instruments");
  auto sessions = root.get("sessions");
  if (instruments.size() == 0 || instruments.size() > 32 ||
      sessions.size() == 0 || sessions.size() > 32)
    throw std::invalid_argument("configure between one and 32 instruments and sessions");
  for (std::size_t index = 0; index < instruments.size(); ++index) {
    const auto id = bounded(instruments.at(index), UINT32_MAX - 1, "instrument");
    if (std::ranges::find(result.instruments, id) != result.instruments.end())
      throw std::invalid_argument("duplicate instrument");
    result.instruments.push_back(static_cast<std::uint32_t>(id));
  }
  std::set<SessionKey> unique;
  for (std::size_t index = 0; index < sessions.size(); ++index) {
    const auto row = sessions.at(index);
    const auto access = row.get("logical_access_id").number();
    const auto partition = row.get("partition_id").number();
    if (access >= UINT32_MAX || partition >= UINT16_MAX)
      throw std::invalid_argument("invalid session identifier");
    SessionRule rule{{static_cast<std::uint32_t>(access), static_cast<std::uint16_t>(partition)}, {}};
    if (!unique.insert(rule.key).second)
      throw std::invalid_argument("duplicate session");
    auto firms = row.get("firms");
    if (firms.size() == 0 || firms.size() > 16)
      throw std::invalid_argument("configure between one and 16 firms per session");
    for (std::size_t slot = 0; slot < firms.size(); ++slot) {
      auto firm = optiq::firm_id(firms.at(slot).string());
      if (std::ranges::find(rule.firms, firm) != rule.firms.end())
        throw std::invalid_argument("duplicate firm");
      rule.firms.push_back(firm);
    }
    result.sessions.push_back(std::move(rule));
  }

  const auto limits = root.get("limits");
  if (!limits.is_null()) {
    auto &l = result.limits;
    const std::tuple<const char *, std::size_t *, std::size_t> entries[]{
        {"live_orders_total", &l.book.live_orders_total, 16384},
        {"live_orders_per_session", &l.book.live_orders_per_session, 1024},
        {"active_prices", &l.book.active_prices, 16384},
        {"matching_max_fills", &l.book.matching_max_fills, 128},
        {"command_queue_capacity", &l.queue, 1024},
        {"reply_slots_per_session", &l.reply_slots, 512},
        {"ordinary_replay_capacity", &l.ordinary_replay, 2048},
        {"notice_slots", &l.notices, 16384},
        {"notice_handoff_slots", &l.notice_handoff, 32},
        {"notice_batch_size", &l.notice_batch, 128},
        {"publication_capacity", &l.publication, 16384},
    };
    for (const auto &[name, target, max] : entries) {
      if (limits.has(name))
        *target = bounded(limits.get(name), max, name);
    }
  }
  const auto &l = result.limits;
  if (l.publication < std::min({l.book.live_orders_total, l.book.live_orders_per_session, l.notices}) ||
      l.publication < 2 + l.book.matching_max_fills)
    throw std::invalid_argument("publication capacity must hold the maximum per-command update batch");
  return result;
}

struct Cores {
  unsigned matcher, publisher, blue;
};

inline std::pair<int, int> physical_core(int cpu) {
  const auto path = "/sys/devices/system/cpu/cpu" + std::to_string(cpu) + "/topology/";
  int package{}, core{};
  if (!(std::ifstream(path + "physical_package_id") >> package) ||
      !(std::ifstream(path + "core_id") >> core))
    throw std::runtime_error("cannot read CPU topology for CPU " + std::to_string(cpu));
  return {package, core};
}

inline Cores choose_cores(const AppOptions &options) {
  cpu_set_t allowed;
  CPU_ZERO(&allowed);
  if (::sched_getaffinity(0, sizeof(allowed), &allowed) != 0)
    throw std::runtime_error("cannot read process CPU affinity");
  std::set<std::pair<int, int>> assigned;
  const auto next = [&](int requested) {
    if (requested >= 0) {
      validate_core(static_cast<unsigned>(requested));
      if (!assigned.insert(physical_core(requested)).second)
        throw std::invalid_argument("gateway, publisher and BlueZone require distinct physical cores");
      return static_cast<unsigned>(requested);
    }
    for (int core = 0; core < 256; ++core) {
      if (CPU_ISSET(core, &allowed) && assigned.insert(physical_core(core)).second)
        return static_cast<unsigned>(core);
    }
    throw std::invalid_argument("at least three distinct physical cores are required");
  };
  // Reserve explicit placements first so an automatic placement cannot occupy them.
  Cores result{};
  if (options.core >= 0)
    result.matcher = next(options.core);
  if (options.publisher_core >= 0)
    result.publisher = next(options.publisher_core);
  if (options.blue_core >= 0)
    result.blue = next(options.blue_core);
  if (options.core < 0)
    result.matcher = next(-1);
  if (options.publisher_core < 0)
    result.publisher = next(-1);
  if (options.blue_core < 0)
    result.blue = next(-1);
  return result;
}

inline std::set<unsigned> cpu_list(const std::string &path) {
  const auto text = read_file(path);
  std::set<unsigned> cpus;
  std::size_t cursor = 0;
  const auto number = [&] {
    unsigned cpu{};
    const auto *start = text.data() + cursor;
    const auto [end, error] = std::from_chars(start, text.data() + text.size(), cpu);
    if (error != std::errc{} || cpu >= CPU_SETSIZE)
      throw std::runtime_error("invalid kernel CPU list: " + path);
    cursor += static_cast<std::size_t>(end - start);
    return cpu;
  };
  while (cursor < text.size() && text[cursor] != '\n') {
    const auto first = number();
    auto last = first;
    if (cursor < text.size() && text[cursor] == '-') {
      ++cursor;
      last = number();
    }
    if (last < first)
      throw std::runtime_error("invalid kernel CPU range: " + path);
    for (auto cpu = first; cpu <= last; ++cpu)
      cpus.insert(cpu);
    if (cursor == text.size() || text[cursor] == '\n')
      break;
    if (text[cursor++] != ',')
      throw std::runtime_error("invalid kernel CPU separator: " + path);
  }
  return cpus;
}

inline void validate_isolation(const Cores &cores) {
  const std::string sysfs = "/sys/devices/system/cpu/";
  const auto isolated = cpu_list(sysfs + "isolated");
  const auto online = cpu_list(sysfs + "online");
  for (auto cpu : {cores.matcher, cores.publisher}) {
    if (!isolated.contains(cpu))
      throw std::runtime_error("RedZone run invalid: reserved CPU is missing from kernel isolation");
    const auto siblings = cpu_list(sysfs + "cpu" + std::to_string(cpu) + "/topology/thread_siblings_list");
    for (auto sibling : siblings) {
      if (sibling != cpu && online.contains(sibling))
        throw std::runtime_error("RedZone run invalid: SMT sibling is still online");
    }
  }
  if (isolated.contains(cores.blue))
    throw std::runtime_error("RedZone run invalid: BlueZone worker requires a housekeeping CPU");
}

} // namespace compute_trade
