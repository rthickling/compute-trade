# Equity calibration and paced GPU workloads

## Reference data

Use the publicly listed Apple (AAPL) ten-level sample for NASDAQ on 21 June
2012. LOBSTER reconstructs the sample from NASDAQ Historical TotalView-ITCH.
The calibration uses 10:00–10:30 New York time, within the regular session.
Download the listed archive locally. Its SHA-256 is
`326839316d67d7819ca0541ffdf73cdb044d6ae4105cce0ef9c615e8848e9d43`.
Keep the source archive and CSV rows local. LOBSTER publishes the sample as a
download, and subscription terms are issued separately. The repository contains
the summary and generation code. [Publisher sample and archive](https://php.lobsterdata.com/info/DataSamples.php) ·
[LOBSTER terms](https://php.lobsterdata.com/termsAndConditions.php)

The sample covers changes within ten occupied prices per side. The message
file identifies adds, partial cancellations, deletions, visible and hidden
executions, cross trades and trading halts within the sample scope. The
depth file gives aggregate displayed shares for each level.
[LOBSTER data structure](https://php.lobsterdata.com/info/DataStructure.php)

| Sample observation | Value |
| --- | --- |
| Rows in the calibration window | 37,837 |
| Adds, partial cancels, full deletes | 18,209 / 229 / 16,333 |
| Visible and hidden execution rows | 1,988 / 1,078 |
| Size of added orders | Median 100 shares, p99 526 shares |
| Visible execution size | Median 83 shares |
| Displayed shares at the best | Median 100 on each side |
| Displayed shares within three one-cent ticks of the best | Median 200 bid, 168 ask |
| Occupied prices in the reported sample | Median 10 per side |
| Quoted spread | Median $0.18 |
| Absolute nonzero midpoint move between adjacent rows | Median $0.015 |
| Observed adds, partial cancels and deletes per second | Median 10, p95 69, p99 108 |
| Observed adds, partial cancels and deletes per 10 ms | p99 5 |

The sample is historical and limited to one venue and the supplied depth.
Message IDs identify individual observed order events. The aggregate
snapshots give quantities at prices, with no count of all resting orders in
those levels. Exchange requests that leave the observed region unaffected
can fall outside the sample. Execution rows measure trades separately from
the add/cancel/delete rate used as a proxy for order traffic.

## Synthetic traces

The two starting books have five prices on each side and three orders per
price. Within three GPU ticks of either best price there are 72 units in the
thin book and 288 in the deep book. Both start at a best bid of 9996 and best
ask of 10004. The experiment holds starting order count and price range
constant to isolate the effect of posted quantity.

The generator maps the observed median add size of 100 shares to five GPU
units and uses a four-tick half-spread. These are workload choices on a
different instrument. Quiet phases mix additions, reductions, cancellations
and small executions near the best prices. A small replenishing order follows
each quiet execution. The default workload offers ten requests per second
in bursts of up to four and runs for approximately 13–17 seconds per
scenario. Ten comes from the median one-second observation. The burst cap
is close to the measured p99 of five events in 10 ms.

Each shock submits four sell requests for 30 units at a limit of 9992, with
identical planned times for thin and deep. The deep book fills all 120 units
and ends the shock at a bid of 9995. The thin book fills 90 units and runs out
of bids. Later buys replenish liquidity. The shock uses no cancellations.
A comparison at four requests for 15 units leaves bids in both books. An
eight-request shock at 15 units again exhausts the thin book. The frozen
default uses four 30-unit requests so the liquidity response differs while
the pressure stays the same. Reproduce the alternative traces with
`--shock-count` and `--shock-size`. A separate tempo can use `--rate 108 --burst 5`
to stress the p99 one-second request rate.

## Reproduction

Run from the repository root. Use a Release build for paced timing. The
commands store the downloaded sample, frozen traces and measurements under
the chosen scratch directory.

```sh
mkdir -p build/workload
curl -fL https://php.lobsterdata.com/info/sample/LOBSTER_SampleFile_AAPL_2012-06-21_10.zip \
  -o build/workload/aapl.zip
python3 tools/workload.py summarize --sample build/workload/aapl.zip \
  --output build/workload/summary.json
python3 tools/workload.py generate --summary build/workload/summary.json \
  --output build/workload/frozen.json
python3 tools/workload.py generate --summary build/workload/summary.json \
  --output build/workload/regenerated.json
CXX=g++-16 cmake --preset release
cmake --build --preset release --parallel
python3 tools/paced_load.py --exchange build/release/bin/ct_exchange \
  --trace build/workload/frozen.json --run-id release-default \
  --output build/workload/runs.json
python3 tests/acceptance/evidence.py workload --trace build/workload/frozen.json \
  --regenerated build/workload/regenerated.json --runs build/workload/runs.json
```

Each run starts a fresh exchange on ephemeral loopback ports, connects a
Level 3 subscriber before prefill, and uses the normal Optiq request entry
point. The generator and subscriber inherit an affinity mask excluding the
two worker physical cores. The script records the role placement, build
hash, trace hash, command timings, matcher durations by operation and phase,
ordered fills, market update count, and final subscriber book parity. The
matcher duration covers book preparation and commit. Diagnostics are enabled
and included in the application environment. Use the matcher-only experiment
for cache comparisons.

Independent due requests can go out while acknowledgements remain
outstanding. If a cancel or amend needs an earlier acknowledgement's
exchange Order ID, the runner waits at that command to preserve the trace's
order. `--window` bounds outstanding requests. Submission lateness is from
the planned send time to the first actual socket attempt. Client round-trip
latency is measured after encoding from that first attempt, through retries
or short writes, to complete decode and correlation of the order reply.
This endpoint measures command acknowledgements. Private fills and public
updates are counted separately.

A drain deadline and the measured counts show any unattempted, unsubmitted
or incomplete requests. A rejected order counts as a completed request.
The script returns a failure if it cannot finish the workload, the matcher
diverges from the reference, or the subscriber fails to reconstruct the
final book. The artefact is written after each run. Use the ordinary affinity
profiles for calibration and separately verify the privileged isolation
profile before interpreting a run as RedZone evidence.

## One-minute paced runs

Generate a longer trace at the sample's p99 one-second rate with
`--rate 108 --burst 5 --duration-seconds 60`. The generator finishes whole
command rounds, so closely spaced schedules can end just after the requested
duration. In this mode each quiet execution replenishes the same maker with
an amendment where possible. That keeps the number of resting orders bounded
without changing the submitted sell pressure. The shock occurs after roughly
45 seconds of quiet activity and is followed by replenishment and a further
period of order churn.

```sh
python3 tools/workload.py generate --summary build/workload/summary.json \
  --rate 108 --burst 5 --duration-seconds 60 --output build/workload/soak.json
python3 tools/workload.py generate --summary build/workload/summary.json \
  --rate 108 --burst 5 --duration-seconds 60 --output build/workload/soak-regenerated.json
python3 tools/paced_load.py --exchange build/release/bin/ct_exchange \
  --trace build/workload/soak.json --run-id release-108-60s \
  --output build/workload/soak-runs.json
python3 tests/acceptance/evidence.py workload --trace build/workload/soak.json \
  --regenerated build/workload/soak-regenerated.json --runs build/workload/soak-runs.json
```

For scenarios over 400 commands, the client streams completion diagnostics in
a background thread. This prevents old command records from being evicted
before verification. The run records which method it used and flags missing
diagnostics as a verification failure. These runs include diagnostic traffic
and show sustained replay correctness at the selected rate.
