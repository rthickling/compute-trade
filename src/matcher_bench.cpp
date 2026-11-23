#include "compute_trade/matcher.hpp"

#include <simplx.h>

#include <algorithm>
#include <array>
#include <charconv>
#include <chrono>
#include <cstdint>
#include <exception>
#include <iostream>
#include <limits>
#include <list>
#include <numeric>
#include <sched.h>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

namespace {
using namespace compute_trade;
using Clock = std::chrono::steady_clock;

enum class Mode { Match, Prepare, Recycle };

struct Settings {
  bool actor_allocator{};
  unsigned cpu{};
  unsigned orders{};
  unsigned fills_per_sweep{};
  unsigned sweeps_per_sample{};
  unsigned samples{};
  Mode mode{};
};

struct Trade {
  std::uint64_t maker_order_id;
  std::uint64_t taker_order_id;
  std::int64_t price;
  std::uint64_t quantity;
};

struct Work {
  std::uint64_t maker_visits{};
  std::uint64_t price_visits{};
  std::uint64_t updates{};
  std::vector<Trade> fills;
};

struct Result {
  std::vector<std::uint64_t> samples_ns;
  std::uint64_t overhead_ns{};
  Work work;
};

std::uint64_t elapsed(Clock::time_point begin, Clock::time_point end) {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(end - begin)
      .count();
}

std::uint64_t timer_overhead() {
  std::array<std::uint64_t, 1001> values{};
  for (auto &value : values) {
    const auto start = Clock::now();
    value = elapsed(start, Clock::now());
  }
  std::sort(values.begin(), values.end());
  return values[values.size() / 2];
}

unsigned number(std::string_view text) {
  unsigned value{};
  const auto [end, error] =
      std::from_chars(text.data(), text.data() + text.size(), value);
  if (error != std::errc{} || end != text.data() + text.size())
    throw std::invalid_argument("invalid unsigned benchmark argument");
  return value;
}

std::string_view mode_name(Mode mode) {
  switch (mode) {
  case Mode::Match:
    return "match";
  case Mode::Prepare:
    return "prepare";
  case Mode::Recycle:
    return "recycle";
  }
  throw std::logic_error("unknown benchmark mode");
}

Settings arguments(int argc, char **argv) {
  const std::string_view allocator(argv[1]);
  if (allocator != "actor" && allocator != "system")
    throw std::invalid_argument("select the actor or system allocator");

  Settings settings{allocator == "actor", number(argv[2]), number(argv[3]),
                    number(argv[4]), number(argv[5]), number(argv[6])};
  if (argc == 8) {
    std::string_view mode(argv[7]);
    if (mode == "prepare")
      settings.mode = Mode::Prepare;
    else if (mode == "recycle")
      settings.mode = Mode::Recycle;
    else if (mode != "match")
      throw std::invalid_argument("select the match, prepare, or recycle mode");
  }
  if (settings.cpu > std::numeric_limits<tredzone::Actor::CoreId>::max() ||
      settings.cpu >= CPU_SETSIZE)
    throw std::invalid_argument("CPU is not addressable by Simplx");
  cpu_set_t allowed;
  if (sched_getaffinity(0, sizeof(allowed), &allowed) != 0 ||
      !CPU_ISSET(settings.cpu, &allowed))
    throw std::invalid_argument("CPU is outside effective affinity");

  if (settings.orders == 0 || settings.orders > 1'048'576 ||
      settings.fills_per_sweep == 0 || settings.fills_per_sweep > 128 ||
      settings.fills_per_sweep > settings.orders ||
      settings.sweeps_per_sample == 0 || settings.sweeps_per_sample > 65'536 ||
      settings.samples == 0 || settings.samples > 8'192)
    throw std::invalid_argument("benchmark setting outside supported limits");

  if (settings.mode == Mode::Prepare &&
      settings.orders % settings.fills_per_sweep != 0)
    throw std::invalid_argument("prepare mode needs a whole number of FIFO groups");

  const auto total =
      std::uint64_t{settings.samples} * settings.sweeps_per_sample *
      settings.fills_per_sweep;
  if (total < settings.orders || total > 2'000'000)
    throw std::invalid_argument(
        "run must touch every starting order and record at most two million fills");
  return settings;
}

template <class Book>
void apply(Book &book, const MatchCommand &command, MatchPlan &plan) {
  book.prepare(command, plan);
  if (!plan.rejection.empty())
    throw std::runtime_error(std::string(plan.rejection));
  book.commit(plan);
}

struct Sweep {
  std::uint64_t maker_visits{};
  std::uint64_t price_visits{};
  std::uint64_t updates{};
};

void record_fills(const MatchPlan &plan, std::size_t expected,
                  std::span<Trade> output) {
  if (plan.fills.size() != expected)
    throw std::logic_error("fixed-fill sweep did not preserve planned work");
  if (output.empty())
    return;
  std::transform(plan.fills.begin(), plan.fills.end(), output.begin(),
                 [](const MatchFill &fill) {
    return Trade{fill.maker.order_id, fill.taker.order_id, fill.price,
                 fill.quantity};
  });
}

// One resting ask FIFO is rotated throughout the run. Each buy consumes the
// same number of makers at the same price, then new sells restore the depth.
template <class Book>
Sweep sweep(Book &book, const MatchCommand &take, const MatchCommand &restore,
            MatchPlan &plan, std::span<Trade> output) {
  apply(book, take, plan);
  record_fills(plan, take.quantity, output);

  const Sweep counts{plan.maker_visits, plan.price_visits,
                     plan.changes.size() + take.quantity};
  for (std::size_t i = 0; i < take.quantity; ++i)
    apply(book, restore, plan);

  return counts;
}

template <class WorkSweep>
void matching_samples(const Settings &settings, Result &result, WorkSweep &&run) {
  result.overhead_ns = timer_overhead();
  result.samples_ns.resize(settings.samples);
  const std::size_t fill_count =
      std::size_t{settings.sweeps_per_sample} * settings.fills_per_sweep;
  result.work.fills.resize(fill_count * settings.samples);
  std::vector<Sweep> sweeps(settings.sweeps_per_sample);
  std::vector<Trade> fills(fill_count);

  for (std::size_t sample = 0; sample < settings.samples; ++sample) {
    const auto begin = Clock::now();
    for (std::size_t i = 0; i < settings.sweeps_per_sample; ++i) {
      sweeps[i] = run(std::span(fills).subspan(i * settings.fills_per_sweep,
                                             settings.fills_per_sweep));
    }
    result.samples_ns[sample] = elapsed(begin, Clock::now());

    for (const Sweep &counts : sweeps) {
      result.work.maker_visits += counts.maker_visits;
      result.work.price_visits += counts.price_visits;
      result.work.updates += counts.updates;
    }
    std::copy(fills.begin(), fills.end(),
              result.work.fills.data() + sample * fill_count);
  }
}

template <class Allocator>
void measure_match(const Settings &settings, const Allocator &allocator,
                   Result &result) {
  const MatchLimits limits{settings.orders, settings.orders, 1,
                           settings.fills_per_sweep};
  const MatchCommand take{MatchKind::New, 1, {'T'}, 1, Side::Buy, 100,
                          settings.fills_per_sweep};
  const MatchCommand restore{MatchKind::New, 0, {'M'}, 1, Side::Sell, 100, 1};
  const auto prefill = [&](auto &book, MatchPlan &plan) {
    for (unsigned i = 0; i < settings.orders; ++i)
      apply(book, restore, plan);
  };

  {
    Matcher<Allocator> warmup({1}, 2, limits, allocator);
    auto plan = warmup.make_plan();
    prefill(warmup, plan);
    const unsigned sweeps = (settings.orders + settings.fills_per_sweep - 1) /
                            settings.fills_per_sweep;
    for (unsigned i = 0; i < sweeps; ++i)
      sweep(warmup, take, restore, plan, {});
  }

  Matcher<Allocator> book({1}, 2, limits, allocator);
  auto plan = book.make_plan();
  prefill(book, plan);
  matching_samples(settings, result, [&](std::span<Trade> out) {
    return sweep(book, take, restore, plan, out);
  });
}

// Rotate through prebuilt one-price instrument groups. Each preparation sees
// the same number of live makers. All book nodes and output storage are
// allocated before timing, and no commits or reclamation run during timing.
template <class Allocator>
void measure_prepare(const Settings &settings, const Allocator &allocator,
                     Result &result) {
  std::vector<std::uint32_t> instruments(settings.orders / settings.fills_per_sweep);
  std::iota(instruments.begin(), instruments.end(), 1U);
  const MatchLimits limits{settings.orders, settings.orders, instruments.size(),
                           settings.fills_per_sweep};
  Matcher<Allocator> book(instruments, 2, limits, allocator);
  auto plan = book.make_plan();
  std::vector<MatchCommand> takes;
  takes.reserve(instruments.size());
  for (const auto instrument : instruments) {
    const MatchCommand sell{MatchKind::New, 0, {'M'}, instrument, Side::Sell,
                            100, 1};
    for (unsigned i = 0; i < settings.fills_per_sweep; ++i)
      apply(book, sell, plan);
    takes.push_back({MatchKind::New, 1, {'T'}, instrument, Side::Buy, 100,
                     settings.fills_per_sweep});
  }

  std::size_t next{};
  const auto run = [&](std::span<Trade> out) {
    const Matcher<Allocator> &live_book = book;
    live_book.prepare(takes[next], plan);
    if (++next == takes.size())
      next = 0;
    if (!plan.rejection.empty())
      throw std::runtime_error(std::string(plan.rejection));
    record_fills(plan, settings.fills_per_sweep, out);
    return Sweep{plan.maker_visits, plan.price_visits, plan.changes.size()};
  };
  for (std::size_t i = 0; i < takes.size(); ++i)
    run({});
  matching_samples(settings, result, run);

  std::uint64_t maker = 1;
  for (const auto &fill : result.work.fills) {
    if (fill.maker_order_id != maker ||
        fill.taker_order_id != std::uint64_t{settings.orders} + 1 ||
        fill.price != 100 || fill.quantity != 1)
      throw std::logic_error("preparation did not follow the fixed FIFO rotation");
    if (++maker > settings.orders)
      maker = 1;
  }
}

// Keep the production FIFO node type, but remove matching and order indexes
// from the timed interval. Validate the actual removed ID sequence off-clock.
template <class Allocator>
void measure_recycle(const Settings &settings, const Allocator &allocator,
                     Result &result) {
  using OrderAllocator = typename std::allocator_traits<
      Allocator>::template rebind_alloc<BookOrder>;
  std::list<BookOrder, OrderAllocator> orders{OrderAllocator(allocator)};
  std::uint64_t next_id{};
  const auto append = [&] {
    ++next_id;
    orders.push_back({next_id, next_id, 1, Side::Sell, 100, 1, 0, {'M'}, 0});
  };
  const auto reset = [&] {
    orders.clear();
    next_id = 0;
    for (unsigned i = 0; i < settings.orders; ++i)
      append();
  };

  reset();
  for (unsigned i = 0; i < settings.orders; ++i) {
    orders.pop_front();
    append();
  }
  reset();

  const std::size_t operations =
      std::size_t{settings.sweeps_per_sample} * settings.fills_per_sweep;
  std::vector<std::uint64_t> retired(operations);
  result.overhead_ns = timer_overhead();
  result.samples_ns.resize(settings.samples);
  std::uint64_t expected_id = 1;
  for (auto &sample : result.samples_ns) {
    const auto begin = Clock::now();
    for (auto &id : retired) {
      id = orders.front().order_id;
      orders.pop_front();
      append();
    }
    sample = elapsed(begin, Clock::now());
    for (const auto id : retired)
      if (id != expected_id++)
        throw std::logic_error("recycling did not preserve the input FIFO");
  }
}

template <class Allocator>
void measure(const Settings &settings, const Allocator &allocator,
             Result &result) {
  switch (settings.mode) {
  case Mode::Match:
    return measure_match(settings, allocator, result);
  case Mode::Prepare:
    return measure_prepare(settings, allocator, result);
  case Mode::Recycle:
    return measure_recycle(settings, allocator, result);
  }
}

struct ActorInput {
  const Settings &settings;
  Result &result;
  std::exception_ptr &error;
};

class BenchmarkActor : public tredzone::Actor {
public:
  explicit BenchmarkActor(const ActorInput &input) {
    try {
      if (sched_getcpu() != static_cast<int>(input.settings.cpu))
        throw std::runtime_error("benchmark worker is on the wrong CPU");

      if (input.settings.actor_allocator)
        measure(input.settings, Allocator<std::byte>(getAllocator()), input.result);
      else
        measure(input.settings, std::allocator<std::byte>{}, input.result);
    } catch (...) {
      input.error = std::current_exception();
    }
  }
};

void report(const Settings &settings, const Result &result) {
  const auto total_sweeps =
      std::uint64_t{settings.samples} * settings.sweeps_per_sample;
  std::cout << "{\"timer_overhead_ns\":" << result.overhead_ns
            << ",\"record_size\":" << sizeof(BookOrder)
            << ",\"cpu\":" << settings.cpu
            << ",\"mode\":\"" << mode_name(settings.mode) << "\""
            << ",\"compiler\":\"" << __VERSION__ << "\""
            << ",\"samples_ns\":[";
  bool separator = false;
  for (const auto sample : result.samples_ns) {
    if (std::exchange(separator, true))
      std::cout << ',';
    std::cout << sample;
  }
  std::cout << "],\"work\":{\"operation_counts\":{";
  if (settings.mode == Mode::Recycle)
    std::cout << "\"recycle\":" << total_sweeps * settings.fills_per_sweep;
  else if (settings.mode == Mode::Prepare)
    std::cout << "\"prepared_new\":" << total_sweeps;
  else
    std::cout << "\"new\":" << total_sweeps + result.work.fills.size();
  std::cout
            << "},\"maker_visits\":" << result.work.maker_visits
            << ",\"price_visits\":" << result.work.price_visits
            << ",\"output_counts\":{";
  if (settings.mode == Mode::Recycle)
    std::cout << "\"verified_retired_ids\":"
              << total_sweeps * settings.fills_per_sweep;
  else if (settings.mode == Mode::Prepare)
    std::cout << "\"planned_book_updates\":" << result.work.updates;
  else
    std::cout << "\"book_updates\":" << result.work.updates;
  std::cout << "},\"fill_count\":" << result.work.fills.size()
            << ",\"ordered_fills\":[";
  separator = false;
  for (const Trade &fill : result.work.fills) {
    if (std::exchange(separator, true))
      std::cout << ',';
    std::cout << '[' << fill.maker_order_id << ',' << fill.taker_order_id << ','
              << fill.price << ',' << fill.quantity << ']';
  }
  std::cout << "]}}\n";
}
} // namespace

int main(int argc, char **argv) {
  if (argc != 7 && argc != 8) {
    std::cerr << "usage: ct_matcher_bench actor|system CPU LIVE_ORDERS FILLS_PER_SWEEP "
                 "SWEEPS_PER_SAMPLE SAMPLES [match|prepare|recycle]\n";
    return 2;
  }
  try {
    const auto settings = arguments(argc, argv);
    Result result;
    std::exception_ptr error;
    {
      tredzone::Engine::CoreSet cores;
      cores.set(static_cast<tredzone::Actor::CoreId>(settings.cpu));
      tredzone::Engine::StartSequence starts(cores);
      starts.addActor<BenchmarkActor>(static_cast<tredzone::Actor::CoreId>(settings.cpu),
                                     ActorInput{settings, result, error});
      tredzone::Engine engine(starts);
    }
    if (error)
      std::rethrow_exception(error);
    if (result.samples_ns.empty())
      throw std::runtime_error("benchmark actor produced no samples");
    report(settings, result);
    return 0;
  } catch (const std::exception &error) {
    std::cerr << "ct_matcher_bench: " << error.what() << '\n';
    return 1;
  }
}
