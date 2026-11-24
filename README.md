# Compute Trade

Compute Trade is a local demonstration exchange for physical GPUs. Instrument
IDs identify separate GPU models. The exchange supports visible limit orders,
cancellation, and cancel-replace over TCP using a subset of Optiq OEG SBE v354.
It matches at the best eligible price and in FIFO order within that price.
Trades execute at the resting order's price. The market data stream publishes
order-level changes using a [demo SBE schema](schema/market-data.xml) over TCP.
A subscriber can reconstruct the Level 3 book.

The exchange is for local experimentation. Session identities and permitted
firms come from its startup configuration. Access to the loopback listener is
the trust boundary.

## Build and test

Use Linux, CMake 3.28 or newer, Ninja, a C++26 compiler, Python 3.10 or newer,
and the json-c development package. On Ubuntu the CMake dependencies include
`pkg-config` and `libjson-c-dev`. The baseline Simplx revision is
`a4030196bb010c0e69797c2ad45d6bd3d44d8dde`. It includes the alignment fix and
the startup fixes for sparse Linux CPU IDs on RedZone boots. Keep a sibling checkout or point
at another checkout with `-DSIMPLX_SOURCE_DIR=/path/to/simplx`.

```sh
git clone https://github.com/rthickling/simplx.git ../simplx
git -C ../simplx switch --detach a4030196bb010c0e69797c2ad45d6bd3d44d8dde
CXX=g++-16 cmake --preset acceptance
cmake --build --preset acceptance --parallel
ctest --test-dir build/acceptance --output-on-failure
```

The application needs three allowed CPUs. It uses distinct physical cores when they exist, and distinct logical CPUs otherwise. The tests launch
ordinary pinned workers and ephemeral loopback sockets. Allow a few seconds
for the test run after the build. The `debug` and `release` presets run matcher,
Simplx, and Optiq startup checks along with framework self-tests. The
`acceptance` preset adds the live Optiq scenarios. See the
[acceptance guide](tests/acceptance/README.md) for individual scenarios and
evidence validators.

ASan and UBSan use the same Simplx revision. The focused `simplx_alignment`
test exercises startup, handler registration and actual Simplx Event delivery.

```sh
CXX=g++-16 cmake --preset sanitizers \
  -DCOMPUTE_TRADE_ACCEPTANCE_LIVE=ON
cmake --build --preset sanitizers --parallel
UBSAN_OPTIONS=print_stacktrace=1 ctest --preset sanitizers
```

The Optiq subset codec is maintained in
[`optiq.hpp`](include/compute_trade/optiq.hpp). The wire oracle uses
independent fixtures derived from the
[published v354 XML](https://connect.euronext.com/sites/default/files/it-documentation/oeg_binary_sbe_input_354.xml).

## Run

Start the exchange from the repository root and pass its configuration file.

```sh
./build/acceptance/bin/ct_exchange --config config/local.json
```

The READY line reports the TCP ports, the worker CPUs and the scheduling
mode. The defaults are loopback ports 9000 for order entry and 9001 for
market data. Start the subscriber in another terminal and wait for its
ready message, then select a configured injector session with `--logical-access`.

```sh
python3 tools/market_subscriber.py --port 9001
```

```sh
./build/acceptance/bin/ct_injector --port 9000 \
  --logical-access 1 --partition 1 --firm FIRM1 --count 10
```

`just exchange` and `just inject` build and run the release exchange and
injector with the sample configuration and session.

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
