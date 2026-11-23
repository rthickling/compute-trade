# RedZone operation on Linux

The configured exchange uses two RedZone physical cores and keeps every other
core for general-purpose work. The matcher reads complete SBE buffers, applies
book changes and writes SBE acknowledgement bytes on one Simplx worker. With
no earlier work or backpressure, those stages run in one Simplx iteration.
BlueZone owns the order-entry TCP listener, accepts connections, assembles
incoming frames and sends private replies. It also samples the clock for wire
timestamps, copies disconnect notices and samples statistics under ordinary
Linux scheduling. The publisher on the second RedZone core encodes SBE
market-data frames. A separate actor on BlueZone owns the market-data socket.
With `--redzone`, Simplx requests real-time FIFO scheduling for the matcher
and publisher. Ordinary scheduling is suitable for correctness tests.

Order-entry input and output cross the core boundary in bounded buffers.
A private reply holds its slot until BlueZone sends its final byte. A close
is sequenced after already admitted work. BlueZone retires the old socket
and its pending writes before its slot can carry another connection.
BlueZone polls the listener and active order sockets together, then services
ready connections with bounded read and write work. The Simplx loop stays
nonblocking so the colocated notice worker can continue to run.

The publisher hands encoded binary frames to BlueZone in sequence. BlueZone
sends up to 32 frames in a socket write. A partially written frame retains its publication
slot. Insufficient publication credit makes the FIFO head wait. If delivery
fails after subscription, the exchange stops so the next run can start with
an empty book and a fresh subscriber.

Book IN Time takes the latest BlueZone UTC sample at matcher entry and
keeps it if the command waits for capacity. Trade Time takes the latest
sample after commit. These wire timestamps approximate the event time and
can repeat or follow a correction to the system clock. Private and market
sequence numbers supply ordering.

## Choose CPUs

Inspect the host with `lscpu -e=CPU,CORE,SOCKET,ONLINE`. Use logical CPU
numbers everywhere. The exchange checks that RedZone workers sit on different
physical cores (including socket identity).

`isolcpus=domain` keeps CPUs out of load balancing. A process launched on
housekeeping CPUs can still explicitly pin its workers to isolated CPUs
that are online and available in its effective cgroup cpuset. The exchange
requests that placement itself. When RedZone CPUs are omitted on the
exchange command line, it looks for isolated online candidates and checks
their effective affinity at startup. [Linux CPU isolation guide](https://docs.kernel.org/admin-guide/cpu-isolation.html#scheduler-domain-isolation)

`tools/redzone_layout.py` picks defaults for any machine:

1. If the kernel already isolates exactly two CPUs, use those.
2. Otherwise take the two highest non-boot physical cores' primary threads.
3. BlueZone defaults to the highest remaining primary thread.
4. Housekeeping is every online CPU except the two RedZone CPUs. The suggested
   boot `irqaffinity` list is the primary threads that remain after `nosmt`.

Override with arguments or `REDZONE_MATCHER`, `REDZONE_PUBLISHER`, and
`REDZONE_BLUE`. Print the layout or the kernel command-line fragment:

```sh
python3 tools/redzone_layout.py
python3 tools/redzone_layout.py --cmdline
eval "$(python3 tools/redzone_layout.py --export)"
```

## just recipes

RedZone recipes live in [`redzone.just`](../redzone.just) and are imported by
the root justfile. From the repository root:

| Recipe | Purpose |
| --- | --- |
| `just redzone-layout` | Show the selected CPUs and suggested cmdline |
| `just redzone-grub` | Install a GRUB menu entry for that layout |
| `just redzone-preflight` | Write layout + status under `build/redzone/` |
| `just redzone-exchange` | Build release and launch with `--redzone` |
| `just redzone-subscribe` | Pin the market subscriber to housekeeping |
| `just redzone-placement` | Verify the running exchange against its startup placement file |
| `just redzone-stop` | Stop exchange/subscriber; restore saved IRQ/workqueue files |
| `just redzone-reboot` | `redzone-stop`, then reboot to the default GRUB entry |

CPU overrides are positional, for example `just redzone-layout 14 12 10`, or
set the `REDZONE_*` environment variables. `just redzone-exchange` also accepts
`config`, `mode`, `port`, and `market_port`.

Typical isolated run after a RedZone boot:

```sh
just redzone-preflight
just redzone-exchange          # terminal A
just redzone-subscribe         # terminal B
just redzone-placement
```

## Prepare an isolated boot

In the bootloader, boot once with these Linux kernel arguments. Replace `M`
and `P` with the matcher and publisher logical CPUs and `HK` with the
post-`nosmt` housekeeping primary list from `redzone_layout.py --cmdline`:

```text
nosmt isolcpus=domain,managed_irq,M,P nohz_full=M,P rcu_nocbs=M,P irqaffinity=HK
```

To install a selectable GRUB entry without changing the default boot:

```sh
just redzone-grub
```

Hold Shift/Esc if needed, then choose **Ubuntu … (Compute Trade RedZone)**.
The default menu entry remains ordinary Ubuntu.

After boot, confirm siblings are offline and the reserved CPUs are online:

```sh
lscpu -e=CPU,CORE,SOCKET,ONLINE
just redzone-preflight
```

The status includes kernel boot settings, the workqueue CPU mask, effective
IRQ targets and power settings. Where needed, move the global workqueue and
movable IRQs to the housekeeping set. Linux uses a comma-separated list of
indices and ranges for `smp_affinity_list`, and a hexadecimal bitmask for
the workqueue `cpumask`. Keep the previous values if making runtime changes
(for example under `build/redzone/workqueue.before` and
`build/redzone/irq-*.before` so `just redzone-stop` can restore them).

```sh
eval "$(python3 tools/redzone_layout.py --export)"
mkdir -p build/redzone
cat /sys/devices/virtual/workqueue/cpumask > build/redzone/workqueue.before
printf '%s\n' "$housekeeping_mask" | sudo tee /sys/devices/virtual/workqueue/cpumask
cat "/proc/irq/$irq/smp_affinity_list" > "build/redzone/irq-$irq.before"
printf '%s\n' "$housekeeping" | sudo tee "/proc/irq/$irq/smp_affinity_list"
```

Apply the IRQ change for each movable IRQ whose effective target includes
a reserved CPU. A managed or per-CPU interrupt may reject the write or
retain its effective target. Document any unavoidable residual IRQ with
the IRQ identity, reason and trace evidence. If `irqbalance` is active,
configure its exclusions or temporarily stop it and record its prior state.
Use a consistent power profile across compared runs and record its settings.

See the Linux kernel [CPU isolation guide](https://docs.kernel.org/admin-guide/cpu-isolation.html)
and [IRQ affinity documentation](https://docs.kernel.org/core-api/irq/irq-affinity.html)
for the kernel controls.

## Launch and record

Prefer the just recipes above. The equivalent manual launch is:

```sh
eval "$(python3 tools/redzone_layout.py --export)"
workers="$PWD/build/redzone/workers.json"
mkdir -p build/redzone
rm -f "$workers"
sudo systemd-run --wait --pipe --collect --uid="$(id -u)" --gid="$(id -g)" \
  -p LimitRTPRIO=10 --working-directory="$PWD" \
  "$PWD/build/release/bin/ct_exchange" --config "$PWD/config/local.json" \
    --bind 127.0.0.1 --port 9000 --market-port 9001 \
    --core "$matcher_cpu" --publisher-core "$publisher_cpu" \
    --blue-core "$blue_cpu" --redzone --placement-file "$workers"
```

The exchange pins the supervisor to BlueZone before starting Simplx. A
short-lived thread constructs the engine and exits before `READY`. Simplx
temporarily pins that thread to each worker CPU for allocation and actor
initialization. The supervisor writes its own process ID, the startup
thread ID and the three worker placements after the startup thread's
`/proc` task entry disappears and before `READY`. RedZone startup
rejects `--acceptance-control`. Use ordinary scheduling for tests that need
snapshots, gates or command traces.

Start the market-data subscriber on housekeeping CPUs and wait for its
ready message before sending the first order. A stream is qualified to
reconstruct from the beginning of one run. Start a new exchange and
subscriber together after a gap or disconnect.

```sh
just redzone-subscribe
# or:
taskset -c "$housekeeping" python3 tools/market_subscriber.py --port 9001 --quiet &
subscriber_pid=$!
```

Pin the test client to housekeeping CPUs in the same way. While the client
and subscriber are active, inspect effective thread affinity and scheduler
policy using their PIDs and the running exchange. Add `--residual-irq`
once per recorded unavoidable IRQ if applicable.

```sh
just redzone-placement
# or:
python3 tools/redzone_status.py --strict --matcher "$matcher_cpu" \
  --publisher "$publisher_cpu" --blue "$blue_cpu" --placement "$workers" \
  --subscriber-pid "$subscriber_pid" --client-pid "$client_pid" > placement.json
```

A nonzero result identifies an invalid configuration or a placement file
whose thread IDs fail verification. The verifier checks that the workers
belong to the running process and that the startup thread has exited. The
`bluezone` row lists order transport, statistics and disconnect notice
copying as responsibilities of its single thread. The matcher prepares
replay frames and BlueZone sends them. The recipe writes the verified
report to `build/redzone/placement.json`.

Capture scheduler and interrupt activity separately, beginning before
exchange startup and continuing during the loaded run. In a separate
terminal, choose a duration that covers startup and the workload.

```sh
sudo trace-cmd record -o redzone.dat -e sched:sched_switch \
  -e irq:irq_handler_entry -e irq:irq_handler_exit sleep 120
sudo trace-cmd report -i redzone.dat > scheduler-irq.txt
```

Review tasks scheduled and interrupts observed on the reserved CPUs. Record
task identities, event loss, trace interval and any residual interruption.
For a trace that includes startup, use `engine_startup_thread_id` from the
placement report to identify Simplx's pre-`READY` activity. If the trace
contains that thread on a reserved CPU during startup, record it in the
evidence manifest's `residual_threads` with that explanation.
Use the [test guide](../tests/acceptance/README.md) to qualify the captured
evidence. Trace collection and high-volume diagnostic controls add work,
so collect timing separately under equivalent placement. Shared last-level
cache, memory bandwidth, power management, firmware and kernel work can
still affect timing. Keep the evidence with the run and build IDs.

## Fresh Event pages during a stalled notice burst

Use a dedicated GCC build to record backing allocations from Simplx's
`WriteCache::newEventPage` fresh-allocation branch. The linker wrap logs
only successful calls at return addresses mapped to that branch by GNU
objdump and addr2line. Event-page reuse takes a different branch. The
positive control forces fresh allocations by queueing 4,096 Events in one
iteration with 2,048-byte Event pages. The exchange retains its normal
Event-page size.

```sh
CXX=g++-16 cmake -S . -B build/event-pages -G Ninja \
  -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCOMPUTE_TRADE_EVENT_PAGE_PROBE=ON \
  -DSIMPLX_SOURCE_DIR=/absolute/path/to/pinned/simplx
cmake --build build/event-pages --parallel --target ct_exchange ct_event_page_probe
python3 tools/measure_event_pages.py --build build/event-pages \
  --output /path/to/new/event-page-run
```

The probe launches the exchange on loopback under ordinary scheduling. It
fills 1,024 live order reservations across 16 sessions while retaining
one older unconfirmed notice. With BlueZone copying paused it disconnects
all 16 sessions, observes four full handoff batches and 896 notices
waiting in reserved storage, and then checks all removals, replay and
confirmations. The timestamps delimit the burst after prefill. The report
records both sending CPUs, explicit counts and capture losses, plus
binary hashes, inline call stacks and the pinned Simplx source revision.
Use this build for allocation evidence and a regular build for latency
measurement. It can run without hardware performance counter or tracing
privileges.

## Rollback

Stop the client and any trace first. Then:

```sh
just redzone-stop      # stop exchange/subscriber; restore saved IRQ/workqueue files
just redzone-reboot    # also reboot into the default (non-RedZone) GRUB entry
```

`nosmt` / `isolcpus` / `nohz_full` only clear after a normal reboot. The
default GRUB entry is ordinary Ubuntu; the RedZone entry is opt-in.
After reboot, verify with `lscpu`, `/proc/cmdline` and the workqueue mask.
