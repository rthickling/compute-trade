// A positive control for fresh Event backing allocations. The actor queues all
// Events before it returns to the loop, so the receiver cannot recycle pages
// while the burst is being published.
#include <simplx.h>

#include <sched.h>

#include <array>
#include <atomic>
#include <chrono>
#include <iostream>
#include <limits>
#include <thread>

namespace {
using tredzone::Actor;
using tredzone::Engine;
constexpr unsigned records = 4096;

struct Payload : Actor::Event {
  std::array<std::byte, 128> payload{};
};

class Burst : public Actor, public Actor::Callback {
public:
  explicit Burst(std::atomic<unsigned> *received) : received_(*received) {
    registerEventHandler<Payload>(*this);
    registerCallback(*this);
  }

  void onCallback() {
    Event::Pipe pipe(*this, getActorId());
    for (unsigned index = 0; index < records; ++index)
      pipe.push<Payload>();
  }

  void onEvent(const Payload &) noexcept {
    received_.fetch_add(1, std::memory_order_relaxed);
  }

private:
  std::atomic<unsigned> &received_;
};

} // namespace

int main() {
  cpu_set_t allowed;
  if (::sched_getaffinity(0, sizeof(allowed), &allowed) < 0)
    return 1;
  for (int cpu = 0;
       cpu < CPU_SETSIZE && cpu <= std::numeric_limits<Actor::CoreId>::max();
       ++cpu) {
    if (!CPU_ISSET(cpu, &allowed))
      continue;
    std::atomic<unsigned> received{};
    const auto core = static_cast<Actor::CoreId>(cpu);
    Engine::CoreSet cores;
    cores.set(core);
    Engine::StartSequence start(cores);
    start.setEventAllocatorPageSizeByte(2048);
    start.addActor<Burst>(core, &received);
    Engine engine(start);
    using namespace std::chrono_literals;
    const auto deadline = std::chrono::steady_clock::now() + 4s;
    while (received.load() < records &&
           std::chrono::steady_clock::now() < deadline)
      std::this_thread::sleep_for(1ms);
    if (received.load() != records) {
      std::cerr << "Simplx positive control did not deliver all Events\n";
      return 1;
    }
    return 0;
  }
  std::cerr << "No allowed CPU can host the positive control\n";
  return 1;
}
