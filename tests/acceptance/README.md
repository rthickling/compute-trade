# Compute Trade acceptance tests

The standard CTest suite runs the existing C++ codec and book tests, the existing
TCP integration suite, and self-tests for the independent acceptance framework.
A new acceptance case starts a real exchange on ephemeral loopback ports. The
Python client reads and writes the published wire format independently of the C++
codec. The reference model uses a scan and sort for matching and compares
ordered fills, displayed book state, and reply semantics. A separate subscriber
reconstructs a Level 3 book from the ordered updates.

## Build and run

Follow the root README to get the pinned Simplx dependency. From the repository
root, use a C++26-capable compiler and Python 3.10 or newer.

```sh
CXX=g++-16 cmake --preset debug
cmake --build --preset debug --parallel
ctest --preset debug
CXX=g++-16 cmake --preset sanitizers -DSIMPLX_SOURCE_DIR=/path/to/fixed/simplx \
  -DCOMPUTE_TRADE_ACCEPTANCE_LIVE=ON
cmake --build --preset sanitizers --parallel
ctest --preset sanitizers
```

The sanitizer preset instruments Compute Trade and the pinned Simplx build with
AddressSanitizer and UndefinedBehaviorSanitizer. It keeps checks separate from
release timing. Use the verified alignment-fixed Simplx revision in the
root README for this build. On the original pinned baseline, GCC's UndefinedBehaviorSanitizer
reports a misaligned `Event::Chain` construction during Simplx startup
(`include/trz/engine/actor.h:2274`, called from `src/engine/node.cpp:272`).
Verbose CTest output with `UBSAN_OPTIONS=print_stacktrace=1` preserves the
stack. Standard CTest takes seconds on an idle machine. An individual
TCP exchange starts on the local machine and gets a four-second I/O deadline
per operation. The harness sends SIGTERM on exit and reports any abnormal
shutdown alongside the exchange output.

The live specification suite is enabled by the acceptance preset and exercises
the exchange with real sockets. The framework self-tests check the independent
oracles and evidence validators with fixed fixtures.

```sh
CXX=g++-16 cmake --preset acceptance
cmake --build --preset acceptance --parallel
ctest --preset acceptance
cd tests
python3 -m acceptance.runner --list
python3 -m acceptance.runner --exchange ../build/acceptance/bin/ct_exchange \
    --case flow.publication_overtake
```

Add `-DSIMPLX_SOURCE_DIR=/absolute/path/to/simplx` at configure time for a
non-sibling dependency checkout. Use the same command with
`--group book`, `--group flow`, or `--group transport` to rerun a suite.
The live tests use TCP and a local Unix socket. They do not require elevated
privileges. The worker placement test requires at least two different physical
worker cores and housekeeping CPUs available to the exchange process.

## Coverage

| Area | Automated check |
| --- | --- |
| Wire and framing | Independent golden bytes for supported v354 messages and session-control frames, malformed headers and groups, mandatory logon, fragmented input, coalesced requests, ordered replies, short writes and retry |
| Matching and ownership | Exact price/time order, partial fills, price sweeps, separate instruments, stale IDs, selected amendment rules, exchange-ID precedence, request reference echo, firm authorisation across sessions, and two fixed-seed tests |
| Limits and ordering | Price and live-order capacity, atomic refusal above the sweep bound, FIFO under reply and publication pressure, full command queue and recovery |
| Disconnect and replay | Command-order disconnect, original session provenance across firms, notices and reservations, saturated handoff, ordinary-reply eviction, retained fills, replay-before-new-work, interrupted replay and confirmation |
| Level 3 | Reconstruct after normal commands and disconnect, compare with the matcher, and invalidate the view on a missing update |
| Runtime | Compare the worker and actual Simplx loop counter at completed frame, match and encoded Ack. Check worker affinity and cross-core Simplx Event placement |
| Later run evidence | Verify regenerated traces, paced-run accounting, matched experimental work, and RedZone host and trace evidence |

The OEG byte fixtures correspond to
[Euronext OEG SBE v5.354.0](https://connect.euronext.com/sites/default/files/it-documentation/Optiq%20OEG%20SBE%20Messages%20-%20Interface%20Specification%20-%20Euronext%20Cash%20and%20Derivatives%20Markets%20-%20External%20-%20v5.354.0.pdf)
and its [v354 schema](https://connect.euronext.com/sites/default/files/it-documentation/oeg_binary_sbe_input_354.xml).
Inputs use the selected continuous-market visible limit fields. The Ack
execution qualifier flags distinguish resting from immediate execution.
Order-specific fills go to each side's session and carry the numeric null
Client Order ID. Session identity, transport state, and request correlation
use their own fields. Business errors use the matching Optiq Reject layout.
The test protocol does not require support for unrelated Optiq operations.

## Interfaces exercised by the suite

An acceptance build starts with these options. Both TCP ports are ephemeral.
The `READY` line includes `port` and `market_port`. The JSON config provides
`instruments`, a list of `sessions` with `logical_access_id`, `partition_id`,
and authorised `firms`, and a `limits` object. Limits absent from a fixture use
the application's normal values. The suite configures two instruments and five
session identities, including two partitions for the same Logical Access.

```text
ct_exchange --bind 127.0.0.1 --port 0 --market-port 0 \
  --config /path/to/exchange.json --acceptance-control /path/to/control.sock
```

The OEG companion messages use the same little-endian ten-byte header, schema 1
and version 1. They have fixed bodies, no groups and no application sequence
of their own.

| Template | Body fields in wire order | Use |
| --- | --- | --- |
| 200 | `sequence` uint32 | Client confirms processing through this sequence, including gaps it recorded |
| 201 | `first_sequence`, `last_sequence` uint32 | Server reports an inclusive missing range at its place in replay |
| 202 | `sequence` uint32 | Server marks the end of recovery at the current private frontier |

A connection logs on with the Optiq templates before application commands.
The ordinary inbound application sequence starts at one and resumes after the
last client message acknowledged by Logon Ack. Give each missed ordinary reply
or retained notice its original private sequence and report missing ranges
in sequence order. ReplayComplete also ends an empty replay. A session may
send new frames during recovery. They cannot affect the book until after replay
completion. A gap marks those order outcomes unknown for the client. The client
can confirm the largest sequence it accounted for and skip duplicate sequences
after interruption.

For market data the independent client sends `{"type":"subscribe"}` followed
by a newline over the market TCP port before sending orders. It receives
`{"type":"ready","sequence":0}` followed by JSON lines with
`sequence`, `command_sequence`, `action` (add, modify, remove), `instrument`,
`order_id`, `side`, `price`, `remaining` and `priority`. Sequence starts
at one across the stream. Removal reports remaining quantity zero. An amend
with fresh priority may be one modify; for multiple updates the book must
reconstruct the same final state with the correct ordered maker changes.
The subscriber refuses deltas after a gap and can start a new exchange run
from the empty state.

## Test controls

The Unix socket is an explicit test interface, available through
`--acceptance-control`. Its requests and responses are newline-terminated
JSON. Each request has an `operation`. A successful response is
`{"ok":true,"result":...}`. An operation that cannot complete within its
deadline returns an error. Use bounded recording and keep diagnostics out of
performance runs. Tests use control barriers to trigger states while sending
real requests through the TCP gateway.

- `gate` sets a named gate, with `value` and optional `logical_access_id`
  and `partition_id`. `publication_credits` limits available update slots.
  `private_send_bytes` gives the selected session a byte budget, preserving
  offsets and producing retryable backpressure after the budget is consumed.
  `private_send`, `notice_copy` and `command_processing` pause their
  respective work when false. `replay_send_through` permits replay through
  an inclusive private sequence and holds later replay and its completion.
  It leaves Logon Ack eligible. A null numeric gate value removes the override.
- `wait` takes `event`, a `match` map and `timeout_ms`. It checks retained
  test events first and returns the matching event object. Field names match
  those of the Python cases. `event_index` increases globally and allows
  tests to verify ordering across actors.
- `snapshot` returns the live `orders` as market records without
  `action`, `sequence`, or `command_sequence`. It also returns
  `queued_command_sequences`, `command_queue_entry_size` and
  `peak_command_queue_depth`. Separate notice counters track current and
  peak occupied slots, current and peak outstanding handoffs, ready records
  waiting for a slot and IDs for batches in flight.
- `placement` returns `workers`. A worker has a `role`, `cpu` and
  `thread_id` as a Linux thread ID. Exchange roles used in the portable
  test are `gateway_matcher`, `publisher`, `statistics`, and
  `disconnect_replay`.

The injector recovery case invokes `ct_injector --replay-only` with the
session's `--logical-access`, `--partition`, and `--firm` in addition to
the host and port. Its `--recovery-state` file supplies the last processed
private sequence and pending Client Order IDs. Its `--report-json` output
records gaps, unknown Client Order IDs, received disconnect Order IDs and
the new processed frontier. This checks the application's own recovery report.

Command events include `command_sequenced`, `command_blocked` and
`command_complete`. They identify commands by `command_sequence`, session
and per-request Client Order ID. A sequenced disconnect has
`kind="disconnect"`. Completion includes every ordered fill and the number
of market updates. A reject that leaves the book untouched has an empty
fill list and zero updates. Background events include notice-batch offer,
copy and release, reply readiness before the first attempted write, notice
confirmation, replay request and replay completion. A replay request marks
`resuming=true` for a returning identity. See the cases for individual field
names and the small capacity settings each test exercises.

Iteration records in `command_complete.stages` are keyed by
`frame_complete`, `book_applied` and `ack_encoded`. Each record contains
`thread_id`, `cpu` and `iteration`, read from the actual executing worker
and Simplx per-core loop counter. A socket write is a later operation with
its own event when blocked. `market_event_received` records sender and receiver
thread and CPU IDs as observed across the actual Simplx Event handoff.

## Verify run evidence

From `tests`, validate a RedZone run using a manifest with the recorded
host state and the text output of `trace-cmd report`. Capture the scheduling
and IRQ trace from before startup through load to see both dedicated workers
scheduled. Worker placement includes the test client and subscriber. The
validator requires actual effective affinity, real-time policy for the two
RedZone workers and offline sibling threads. Record movable interrupt
affinities and explain residual threads and interrupts observed on those CPUs.
Kernel, power settings, number of completed commands and build/run IDs
identify the observations.

```sh
python3 -m acceptance.hardware --capture placement.json
python3 -m acceptance.hardware --verify redzone-manifest.json scheduler-irq.txt
```

Use the capture output as the manifest's `host` section. The manifest also
records any verified cgroup v2 isolated CPUs and a `notice_burst` observation
at the largest permitted live-order count with the notice reader delayed.
Record fresh backing allocation from Simplx's Event allocator on each
sending RedZone core, the probe and sample losses, the notice capacity,
previously unconfirmed notices, effective live-order limit and handoff peaks.
A recycled page is distinct from a fresh allocation.
The fixture in `test_framework.py` shows the manifest fields.

From the repository root, verify the workload and matcher artifacts with the
evidence checker. Use reproducibly generated inputs and measurements from the
actual build. These checks require the run artifacts as inputs.

```sh
python3 tests/acceptance/evidence.py workload --trace frozen.json \
  --regenerated regenerated.json --runs paced-runs.json
python3 tests/acceptance/evidence.py matcher --runs matcher-runs.json
```

The evidence module documents the expected records. Keep load and cache
measurement runs separate from diagnostic and sanitizer builds.
