# Compute Trade

Compute Trade is a local demonstration exchange for physical GPUs. Instrument
IDs identify separate GPU models. The exchange supports visible limit orders,
cancellation, and cancel-replace over TCP using a subset of Optiq OEG SBE v354.
It matches at the best eligible price and in FIFO order within that price.
Trades execute at the resting order's price. The market data stream publishes
order-level changes and a subscriber can reconstruct the Level 3 book.

The exchange is for local experimentation. Session identities and permitted
firms come from its startup configuration. Access to the loopback listener is
the trust boundary.

## Build and test

Use Linux, CMake 3.28 or newer, Ninja, a C++26 compiler, Python 3.10 or newer,
and the json-c development package. On Ubuntu the CMake dependencies include
`pkg-config` and `libjson-c-dev`. The baseline Simplx revision is
`c0b2ca712f1c187b6c120d5bcaf441bdff1600ae`. Keep a sibling checkout or point
at another checkout with `-DSIMPLX_SOURCE_DIR=/path/to/simplx`.

```sh
git clone https://github.com/rthickling/simplx.git ../simplx
git -C ../simplx switch --detach c0b2ca712f1c187b6c120d5bcaf441bdff1600ae
CXX=g++-16 cmake --preset acceptance
cmake --build --preset acceptance --parallel
ctest --test-dir build/acceptance --output-on-failure
```

The application needs three allowed physical cores. The tests launch
ordinary pinned workers and ephemeral loopback sockets. Allow a few seconds
for the test run after the build. The `debug` and `release` presets run component
checks, the original socket suite, and framework self-tests. The
`acceptance` preset adds the live Optiq scenarios. See the
[acceptance guide](tests/acceptance/README.md) for individual scenarios and
evidence validators.

For ASan and UBSan, point the sanitizer build at an unmodified checkout of
Simplx `8b57604f68150ebf929ebecff0b895f263b8e2c3`, which includes the engine
alignment fix. The original revision fails UBSan during engine startup.
The focused `simplx_alignment` test exercises startup, handler registration
and actual Simplx Event delivery.

```sh
CXX=g++-16 cmake --preset sanitizers -DSIMPLX_SOURCE_DIR=/path/to/fixed/simplx \
  -DCOMPUTE_TRADE_ACCEPTANCE_LIVE=ON
cmake --build --preset sanitizers --parallel
UBSAN_OPTIONS=print_stacktrace=1 ctest --preset sanitizers
```

The Optiq subset codec is maintained in
[`optiq.hpp`](include/compute_trade/optiq.hpp). The wire oracle uses
independent fixtures derived from the
[published v354 XML](https://connect.euronext.com/sites/default/files/it-documentation/oeg_binary_sbe_input_354.xml).
The earlier Compute Trade SBE schema and its generated headers remain for
the original compatibility mode. Regenerate those with
`python3 tools/generate_sbe.py` and check with
`python3 tools/generate_sbe.py --check`. Both commands require Java 17 or
newer when regenerating. Use `--jar` for an offline copy of the pinned
1.41.0 generator.

## Run

Start the exchange from the repository root with the sample configuration.

```sh
./build/acceptance/bin/ct_exchange --config config/local.json
```

The READY line reports the TCP ports, the worker CPUs and the scheduling
mode. The defaults are loopback ports 9000 for order entry and 9001 for
market data. Start the subscriber in another terminal and wait for its
ready message, then start a configured injector.

```sh
python3 tools/market_subscriber.py --port 9001
```

```sh
./build/acceptance/bin/ct_injector --port 9000 \
  --logical-access 1 --partition 1 --firm FIRM1 --count 10
```

Orders match or rest and the subscriber prints the resulting updates.
An injector disconnect cancels its live orders and publishes those removals.
Stop the exchange with Ctrl-C and then stop the subscriber. A stream gap or
subscriber loss requires a fresh run with an empty book.

See [Exchange architecture and limits](docs/architecture.md) for request
correlation, limits, recovery and overload handling. The [workload guide](docs/workloads.md)
and [matcher benchmark](docs/matcher-benchmark.md) describe reproducible
measurements.

### RedZone

Isolated real-time runs use two reserved physical cores; all other cores stay
general-purpose. CPU selection, a GRUB menu entry, launch, placement checks and
rollback are wrapped as `just` recipes in [`redzone.just`](redzone.just). See
the [RedZone guide](docs/redzone.md) for the full procedure.

```sh
just redzone-layout          # portable defaults for this host
just redzone-grub            # optional GRUB entry (default boot unchanged)
just redzone-preflight       # after a RedZone boot
just redzone-exchange        # RT launch
just redzone-subscribe
just redzone-placement
just redzone-stop            # or: just redzone-reboot
```

The original TCP protocol remains available when the exchange is started
without `--config`. Start the injector without `--logical-access` for that
mode. This preserves the earlier development and test workflow.
