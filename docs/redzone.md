# RedZone operation on Linux

The configured exchange can use three separate physical cores. The matcher
completes frame assembly, decode, book changes and acknowledgement encoding
on its own Simplx worker. The publisher owns the market-data socket on the
second worker. The third worker copies disconnect notices and samples
statistics. With `--redzone`, Simplx requests real-time FIFO scheduling
for the matcher and publisher. Its BlueZone worker uses normal Linux
scheduling. Ordinary scheduling is suitable for correctness tests.

## Prepare an isolated boot

Inspect the host with `lscpu -e=CPU,CORE,SOCKET,ONLINE`. Pick two usable
non-boot CPUs on different physical cores and a third physical core for
the BlueZone worker. Leave enough CPUs for the desktop, client and subscriber.
Use logical CPU numbers for all flags. The physical core check in the exchange
includes socket identity.

In the bootloader, temporarily append these Linux kernel arguments, replacing
`M` and `P` with the matcher and publisher logical CPU numbers and `HK` with
the housekeeping CPU list. A one-boot edit in GRUB keeps the installed
boot configuration intact.

```text
nosmt isolcpus=domain,managed_irq,M,P nohz_full=M,P rcu_nocbs=M,P irqaffinity=HK
```

Run `lscpu -e=CPU,CORE,SOCKET,ONLINE` again after boot. Confirm that the chosen
CPUs are online and their siblings have gone offline. Select housekeeping
CPUs from the resulting online list. Use the status script first
with the actual IDs supplied in your shell.

```sh
python3 tools/redzone_status.py --matcher "$matcher_cpu" \
  --publisher "$publisher_cpu" --blue "$blue_cpu" > preflight.json
```

The status includes kernel boot settings, the workqueue CPU mask, effective
IRQ targets and power settings. Where needed, move the global workqueue and
movable IRQs to the housekeeping set. Linux uses a comma-separated list of
indices and ranges for `smp_affinity_list`, and a hexadecimal bitmask for
the workqueue `cpumask`. Keep the previous values if making runtime changes.
For example, on a host whose housekeeping list is CPUs 0, 4 and 6, the list is
`0,4,6` and the bitmask is `51`.

```sh
cat /sys/devices/virtual/workqueue/cpumask > workqueue.before
printf '%s\n' "$housekeeping_mask" | sudo tee /sys/devices/virtual/workqueue/cpumask
cat "/proc/irq/$irq/smp_affinity_list" > "irq-$irq.before"
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

Use a functional run first with normal privileges. For a real-time run, grant
the launch user the applicable real-time priority limit or use a supervised
transient systemd service. Keep the gateway local. Start the process from
the repository root with the ports, session configuration and CPU IDs used
in the run. Use a private directory for the optional control socket, and
enable it when collecting placement diagnostics.

```sh
control_dir=$(mktemp -d)
control="$control_dir/placement.sock"
sudo systemd-run --wait --pipe --collect --uid="$(id -u)" --gid="$(id -g)" \
  -p LimitRTPRIO=10 --working-directory="$PWD" \
  "$PWD/build/release/bin/ct_exchange" --config "$PWD/config/local.json" \
    --bind 127.0.0.1 --port 9000 --market-port 9001 \
    --core "$matcher_cpu" --publisher-core "$publisher_cpu" \
    --blue-core "$blue_cpu" --redzone --acceptance-control "$control"
```

Start the market-data subscriber on housekeeping CPUs and wait for its
ready message before sending the first order. A stream is qualified to
reconstruct from the beginning of one run. Start a new exchange and
subscriber together after a gap or disconnect.

```sh
taskset -c "$housekeeping" python3 tools/market_subscriber.py --port 9001 --quiet &
subscriber_pid=$!
```

Pin the test client to housekeeping CPUs in the same way. While the client
and subscriber are active, inspect effective thread affinity and scheduler
policy using their PIDs and the running exchange. Add `--residual-irq`
once per recorded unavoidable IRQ if applicable.

```sh
python3 tools/redzone_status.py --strict --matcher "$matcher_cpu" \
  --publisher "$publisher_cpu" --blue "$blue_cpu" --control "$control" \
  --subscriber-pid "$subscriber_pid" --client-pid "$client_pid" > placement.json
```

A nonzero result identifies an invalid configuration. A successful result
verifies the sampled settings. Capture scheduler and interrupt activity
separately, beginning before exchange startup and continuing during the
loaded run. In a separate terminal, choose a duration that covers startup
and the workload.

```sh
sudo trace-cmd record -o redzone.dat -e sched:sched_switch \
  -e irq:irq_handler_entry -e irq:irq_handler_exit sleep 120
sudo trace-cmd report -i redzone.dat > scheduler-irq.txt
```

Review tasks scheduled and interrupts observed on the reserved CPUs. Record
task identities, event loss, trace interval and any residual interruption.
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

Stop the client, then stop the exchange service, subscriber and trace.
Restore any changed `smp_affinity_list` and workqueue `cpumask` from their
saved files using the same write commands. Restore the previous
`irqbalance` and power settings. Reboot normally to discard the one-boot
kernel flags and restore the boot-time CPU topology. Verify the restored
state with `lscpu`, `/proc/cmdline` and the workqueue mask.
