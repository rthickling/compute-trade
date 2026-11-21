// Exercise the Simplx startup and event path used by the exchange with no TCP
// or scheduling privileges. ASan and UBSan instrument the original engine code.
#include <simplx.h>

#include <sched.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <thread>

namespace {

using tredzone::Actor;
using tredzone::Engine;

struct ProbeEvent : Actor::Event {
  const unsigned value;
  explicit ProbeEvent(unsigned observed) noexcept : value(observed) {}
};

struct State {
  Actor::ActorId destination;
  std::atomic<unsigned> delivered{};
  std::atomic<int> sender_cpu{-1};
  std::atomic<int> sender_tid{-1};
  std::atomic<int> receiver_cpu{-1};
  std::atomic<int> receiver_tid{-1};
};

class Receiver : public Actor {
public:
  explicit Receiver(State *state) : state_(*state) {
    state_.destination = getActorId();
    registerEventHandler<ProbeEvent>(*this);
    registerUndeliveredEventHandler<ProbeEvent>(*this);
  }

  void onEvent(const ProbeEvent &event) noexcept {
    state_.receiver_cpu.store(::sched_getcpu(), std::memory_order_relaxed);
    state_.receiver_tid.store(::gettid(), std::memory_order_relaxed);
    state_.delivered.store(event.value, std::memory_order_release);
  }

  void onUndeliveredEvent(const ProbeEvent &) noexcept {
    state_.delivered.store(1, std::memory_order_release);
  }

private:
  State &state_;
};

class Sender : public Actor, public Actor::Callback {
public:
  explicit Sender(State *state) : state_(*state) { registerCallback(*this); }

  void onCallback() {
    state_.sender_cpu.store(::sched_getcpu(), std::memory_order_relaxed);
    state_.sender_tid.store(::gettid(), std::memory_order_relaxed);
    Event::Pipe(*this, state_.destination).push<ProbeEvent>(42);
  }

private:
  State &state_;
};

bool run() {
  cpu_set_t allowed;
  if (::sched_getaffinity(0, sizeof(allowed), &allowed) < 0)
    throw std::runtime_error("cannot inspect test process affinity");

  int first = -1;
  int second = -1;
  for (int cpu = 0; cpu <= std::numeric_limits<Actor::CoreId>::max() && cpu < CPU_SETSIZE; ++cpu) {
    if (!CPU_ISSET(cpu, &allowed))
      continue;
    if (first < 0) {
      first = cpu;
      continue;
    }
    second = cpu;
    break;
  }
  if (first < 0)
    throw std::runtime_error("no usable Simplx CPU in the process affinity");
  if (second < 0)
    second = first;

  const auto sender = static_cast<Actor::CoreId>(first);
  const auto receiver = static_cast<Actor::CoreId>(second);
  Engine::CoreSet cpus;
  cpus.set(sender);
  cpus.set(receiver);
  State state;
  Engine::StartSequence start(cpus);
  start.addActor<Receiver>(receiver, &state);
  start.addActor<Sender>(sender, &state);
  Engine runtime(start);
  using namespace std::chrono_literals;
  const auto deadline = std::chrono::steady_clock::now() + 3s;
  while (!state.delivered.load(std::memory_order_acquire) &&
         std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(1ms);

  return state.delivered.load(std::memory_order_acquire) == 42 &&
         state.sender_cpu.load(std::memory_order_relaxed) == sender &&
         state.receiver_cpu.load(std::memory_order_relaxed) == receiver &&
         ((sender == receiver) ==
          (state.sender_tid.load(std::memory_order_relaxed) ==
           state.receiver_tid.load(std::memory_order_relaxed)));
}

} // namespace

int main() {
  try {
    if (run())
      return 0;
    std::cerr << "Simplx did not dispatch the startup probe on its expected workers\n";
  } catch (const std::exception &exception) {
    std::cerr << exception.what() << '\n';
  }
  return 1;
}
