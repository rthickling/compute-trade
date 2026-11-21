# Matcher allocator benchmark

Build the release preset and select one permitted CPU.

```sh
CXX=g++-16 cmake --preset release
cmake --build --preset release --target ct_matcher_bench
python3 tools/run_matcher_bench.py --executable build/release/bin/ct_matcher_bench \
  --cpu 4 --orders 256 4096 32768 262144 --pairs 2 \
  --report matcher-exploration.json
python3 tests/acceptance/evidence.py matcher --runs matcher-exploration.json
```

The CPU number is an example. Check the current shell affinity with
`taskset -pc $$`. Run the same command with `--phase confirmation` and a
different report path for a fresh run after choosing the comparison. Add
`--figure matcher.svg` to generate the spread and paired differences if
Matplotlib is available. To regenerate the figure from a saved JSON or gzip
JSON report, use `python3 tools/run_matcher_bench.py --plot-report matcher-exploration.json --figure matcher.svg`.
Larger runs store every fill in the JSON evidence.

The command uses the same matcher, list, lookup and fill recording for both
allocators on a pinned Simplx worker. A separate process runs each trial.
The wrapper alternates actor/system and system/actor execution order.
The binary SHA-256 identifies the exact executable. The trace digest records
the prefill and the work pattern.

Every starting order is a one-unit sell at one price. A sweep executes against
the oldest makers and the same number of one-unit sell orders restores the
level. Each timed run rotates through the entire starting FIFO at least once.
Use `--fills 4` for a separate fixed-footprint run that changes the work per
sweep. The maximum accepted working-set value is 1,048,576 live orders. The
case value counts bytes in the live order records and excludes links, index
storage, and allocator metadata. Treat it as the approximate scale of the
live record working set.

The timer covers matching preparation, commits for the crossing and restoration
commands, and copying fills into reused batch storage. The program constructs
the book and runs one full FIFO rotation as a warm-up before rebuilding the
initial state. It records timer overhead separately. The run excludes TCP,
SBE decoding, reply encoding, publication, private notice work, and statistics
aggregation. Reported samples are whole-batch times in nanoseconds. Divide by
`sweeps_per_sample` for the time per sweep and restoration. The verifier
compares exact ordered fills, visit counts and output counts within each pair.

Run hardware observations separately. The generated report identifies the
ordinary scheduling configuration and records timings only. Allocator results
include both allocation/recycling and matcher traversal.

## Separate controls

```sh
python3 tools/run_matcher_bench.py --executable build/release/bin/ct_matcher_bench \
  --cpu 4 --mode prepare --fills 4 --orders 256 4096 32768 262144 \
  --pairs 2 --report matcher-prepare.json
python3 tests/acceptance/evidence.py matcher --runs matcher-prepare.json
python3 tools/run_matcher_bench.py --executable build/release/bin/ct_matcher_bench \
  --cpu 4 --mode recycle --fills 4 --orders 256 4096 32768 262144 \
  --pairs 2 --report matcher-recycle.json
python3 tests/acceptance/evidence.py matcher --runs matcher-recycle.json
```

`prepare` prebuilds one price per instrument and exactly `--fills` one-unit
FIFO makers per price. It rotates through the instruments and calls the
production `Matcher::prepare` on a const book. All nodes and scratch storage
exist before the clock starts. Each operation visits the same number of
makers, visits one eligible price and copies prospective fills in the same
order. The control measures preparation including live-node traversal, instrument
and price lookup, and output copying. The size sweep also changes the number of
instruments and indexed levels.

`recycle` times removal and append using the production
`std::list<BookOrder>` node type and the chosen allocator. It preserves
depth, checks the sequence of removed order IDs outside the timed
interval, and exercises allocation and recycling without matching or indexes.
The chart reports time per recycled node.

The controls help locate which effects warrant a targeted experiment. Their
layouts and timed work differ from `match`, so subtracting the control timings
from the matcher totals would mix different work and cache histories.
