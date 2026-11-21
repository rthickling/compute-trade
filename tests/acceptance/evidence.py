"""Validate paced-load and matcher-only evidence without timing thresholds.

All inputs are JSON. For `workload`, --trace and --regenerated contain
`near_best_ticks` (a nonnegative price band) and a `scenarios` object with
`thin_quiet`, `deep_quiet`, `thin_shock`, and `deep_shock`. Each scenario
contains its instrument, seed, starting orders, commands, and observations.
Orders carry instrument, side (1/2), price, quantity, and order_id. Commands
carry id, operation (new/cancel/replace), phase (quiet/shock/replenishment),
and planned_ns relative to the load origin. New and replace commands also
carry instrument, side, price, and quantity. Each command has an observation
with id, best_bid, best_ask, available_buy_quantity, ordered fills (price and
quantity plus identity), and prices_crossed. Shock cancellations additionally
record cancel_rule in the scenario and cancelled_quantity in the observation.

The --runs file contains a `runs` array. Each workload run identifies run_id,
build_id, scenario, and trace_id (digest of initial_book and commands). Its
requests match the command IDs in scheduled order. Each contains zero or more
send_attempts_ns. A request can also carry encoded_ns, submitted_ns (full
frame accepted by the socket), reply_decoded_ns, reply_correlated_ns, outcome
(accepted/rejected), matcher_ns, lateness_ns and rtt_ns. Timestamps use the
same relative origin as planned_ns. A run supplies drain_until_ns, counts,
peak_outstanding, backlog_drained, and market_data_enabled. Request counts
refer to distinct commands, completed includes rejected, and incomplete
counts submitted commands still awaiting a terminal reply. The peak groups
submissions and replies at an identical nanosecond and conservatively counts
submissions first. Runs sharing comparison_id also supply ordered_fills and
output_counts so their completed work can be compared.

For `matcher`, --runs contains environment, timing, and an ordered `pairs`
array. Timing records start, stop, overhead_ns, exclusions, fill_recording,
event_preparation_included, and notice_preparation_included. Each pair has
case_id, phase (exploration/confirmation), working_set_bytes, trace_id and two
runs in execution order. The trace ID identifies both the starting book and
commands. Each run has run_id, variant, build_id, trace_id, timing_only, work
(ordered fills and fill_count, operation_counts, maker_visits, price_visits,
and output_counts), raw
samples_ns and sample_count. A case repeated under the same conditions must
include both run orders. Optional cache_observations is a list. Each entry
records either a reason for unavailability or the measurement method, its
own run_id, a timing_run_id, the identical build_id, event, filter,
instruction map, raw-data reference, sample and loss counts, denominator,
and cache-source counts. Paired differences subtract the median for the
first variant in lexical order from the second, regardless of execution
order. Inspect the returned counts when comparing work across footprints.
"""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from statistics import median


SCENARIOS = ("thin_quiet", "deep_quiet", "thin_shock", "deep_shock")
OPERATIONS = ("new", "cancel", "replace")
PHASES = ("quiet", "shock", "replenishment")
TIMING_EXCLUSIONS = {
    "tcp", "sbe_decode", "acknowledgements", "publication", "notice_replay", "statistics",
}


def require(condition, context, requirement):
    if not condition:
        raise ValueError(f"{context}: {requirement}")


def nonnegative(value, context):
    require(type(value) is int and value >= 0, context, "expected a nonnegative integer")
    return value


def positive(value, context):
    require(nonnegative(value, context) > 0, context, "expected a positive integer")
    return value


def identity(value, context):
    require(isinstance(value, str) and value.strip(), context, "expected a nonempty ID")
    return value


def validate_counts(counts, context):
    require(isinstance(counts, dict) and counts, context, "expected named counts")
    for field, count in counts.items():
        nonnegative(count, f"{context}.{field}")


def trace_id(scenario):
    inputs = {field: scenario[field] for field in ("initial_book", "commands")}
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def starting_liquidity(book, instrument, band, context):
    prices = {1: [], 2: []}
    order_ids = set()
    for order in book:
        positive(order["quantity"], context)
        positive(order["price"], context)
        require(order["side"] in prices, context, "invalid side in starting book")
        key = order["instrument"], order["order_id"]
        require(key not in order_ids, context, "duplicate order ID in starting book")
        order_ids.add(key)
        if order["instrument"] == instrument:
            prices[order["side"]].append((order["price"], order["quantity"]))
    require(prices[1] and prices[2], context, "starting book must have both sides")
    bid = max(price for price, _ in prices[1])
    ask = min(price for price, _ in prices[2])
    require(bid < ask, context, "starting book is crossed")
    return (
        sum(quantity for price, quantity in prices[1] if price >= bid - band),
        sum(quantity for price, quantity in prices[2] if price <= ask + band),
    )


def observations(scenario, name):
    commands = scenario["commands"]
    observed = scenario["observations"]
    require(commands and len(commands) == len(observed), name, "need one observation per command")
    command_ids = set()
    previous_time = -1
    pressure = []
    shock_cancelled = []
    for command, result in zip(commands, observed, strict=True):
        command_id = command["id"]
        context = f"{name}/{command_id}"
        require(command_id not in command_ids and result["id"] == command_id,
                context, "duplicate command or mismatched observation ID")
        command_ids.add(command_id)
        timestamp = nonnegative(command["planned_ns"], context)
        require(timestamp >= previous_time, context, "commands must be scheduled in order")
        previous_time = timestamp
        operation, phase = command["operation"], command["phase"]
        require(operation in OPERATIONS and phase in PHASES, context, "unknown operation or phase")
        for field in ("best_bid", "best_ask"):
            if result[field] is not None:
                positive(result[field], f"{context}.{field}")
        nonnegative(result["available_buy_quantity"], context)
        for fill in result["fills"]:
            positive(fill["price"], f"{context} fill price")
            positive(fill["quantity"], f"{context} fill quantity")
        crossed = list(dict.fromkeys(fill["price"] for fill in result["fills"]))
        require(result["prices_crossed"] == crossed, context, "crossed prices do not match ordered fills")
        if operation in ("new", "replace"):
            require(command["side"] in (1, 2), context, "invalid order side")
            positive(command["quantity"], context)
            positive(command["price"], context)
            if phase == "shock" and command["side"] == 2:
                pressure.append((timestamp, operation, command["instrument"], command["price"], command["quantity"]))
        if phase == "shock" and operation == "cancel":
            shock_cancelled.append(nonnegative(result["cancelled_quantity"], context))
    actual_phases = {command["phase"] for command in commands}
    if name.endswith("_shock"):
        require(pressure and "replenishment" in actual_phases, name, "need sell pressure and replenishment")
        levels = {"quiet": 0, "shock": 1, "replenishment": 2}
        phases = [levels[command["phase"]] for command in commands]
        require(phases == sorted(phases), name, "replenishment must follow the sell shock")
    else:
        require(actual_phases == {"quiet"}, name, "quiet workload contains a shock phase")
    return pressure, shock_cancelled


def validate_traces(trace, regenerated):
    # JSON round trips normalize key order but preserve book/command/fill order.
    require(trace == regenerated, "trace regeneration", "regenerated evidence differs")
    scenarios = trace["scenarios"]
    require(set(scenarios) == set(SCENARIOS), "traces", "expected all four scenarios")
    band = nonnegative(trace["near_best_ticks"], "near_best_ticks")
    liquidity = {}
    scripted_pressure = {}
    cancelled_units = {}
    for name in SCENARIOS:
        scenario = scenarios[name]
        require(type(scenario["seed"]) is int, name, "seed must be an integer")
        liquidity[name] = starting_liquidity(scenario["initial_book"], scenario["instrument"], band, name)
        pressure, cancelled = observations(scenario, name)
        scripted_pressure[name] = pressure
        if cancelled:
            require(scenario.get("cancel_rule"), name, "missing shock cancellation rule")
        cancelled_units[name] = sum(cancelled)
    for depth in ("thin", "deep"):
        quiet = scenarios[f"{depth}_quiet"]
        shock = scenarios[f"{depth}_shock"]
        require(quiet["initial_book"] == shock["initial_book"] and quiet["instrument"] == shock["instrument"],
                depth, "quiet and shock runs must start from the same book")
    require(all(a < b for a, b in zip(liquidity["thin_quiet"], liquidity["deep_quiet"], strict=True)),
            "starting books", "deep must contain more near-best quantity on both sides")
    require(scripted_pressure["thin_shock"] == scripted_pressure["deep_shock"],
            "shock traces", "planned sell timing, size, price limit, or operation differs")
    require(scenarios["thin_shock"].get("cancel_rule") == scenarios["deep_shock"].get("cancel_rule"),
            "shock traces", "cancellation rules differ")
    return {
        name: {"trace_id": trace_id(scenarios[name]), "near_best_buy_quantity": liquidity[name][0],
               "near_best_sell_quantity": liquidity[name][1], "shock_cancelled_units": cancelled_units[name]}
        for name in SCENARIOS
    }


def request_timeline(command, row, context, drain_until):
    attempts = row["send_attempts_ns"]
    encoded = row.get("encoded_ns")
    submitted = row.get("submitted_ns")
    decoded = row.get("reply_decoded_ns")
    correlated = row.get("reply_correlated_ns")
    outcome = row.get("outcome")
    for stamp in attempts:
        nonnegative(stamp, context)
    for stamp in (encoded, *attempts, submitted, decoded, correlated):
        if stamp is not None:
            require(nonnegative(stamp, context) <= drain_until, context, "event after drain deadline")
    if attempts:
        require(encoded is not None and encoded <= attempts[0], context, "first send attempt precedes encoding")
        require(attempts == sorted(attempts), context, "send attempt times are out of order")
        require(row.get("lateness_ns") == attempts[0] - command["planned_ns"],
                context, "lateness must use the first send attempt")
    else:
        require(row.get("lateness_ns") is None, context, "unattempted command has lateness")
    if submitted is not None:
        require(attempts and attempts[-1] <= submitted, context, "submission precedes last send attempt")
    if decoded is not None:
        require(submitted is not None and submitted <= decoded, context, "decoding precedes full submission")
    if correlated is not None:
        require(decoded is not None and decoded <= correlated, context, "correlation precedes decoding")
        require(outcome in ("accepted", "rejected"), context, "correlated reply lacks an outcome")
        if row.get("rtt_ns") is not None:
            require(row["rtt_ns"] == correlated - attempts[0], context, "RTT must use the first send attempt")
    else:
        require(outcome is None and row.get("rtt_ns") is None, context, "uncompleted request has an outcome or RTT")
    matcher_ns = row.get("matcher_ns")
    if matcher_ns is not None:
        require(submitted is not None, context, "matcher timing without a submitted command")
        nonnegative(matcher_ns, context)
    return submitted, correlated


def validate_load_run(run, scenario):
    run_id = identity(run["run_id"], "workload run_id")
    identity(run["build_id"], run_id)
    require(run["trace_id"] == trace_id(scenario), run_id, "trace ID does not identify the supplied inputs")
    require(run["market_data_enabled"] is True, run_id, "market data was disabled")
    drain_until = nonnegative(run["drain_until_ns"], run_id)
    commands = scenario["commands"]
    rows = run["requests"]
    require(len(commands) == len(rows), run_id, "each planned command needs a request record")
    counts = dict(planned=len(commands), attempted=0, submitted=0, completed=0, rejected=0, incomplete=0)
    activities = Counter()
    by_operation = defaultdict(list)
    for command, row in zip(commands, rows, strict=True):
        context = f"{run_id}/{command['id']}"
        require(command["id"] == row["id"], context, "request ID differs from plan")
        submitted, correlated = request_timeline(command, row, context, drain_until)
        counts["attempted"] += bool(row["send_attempts_ns"])
        counts["submitted"] += submitted is not None
        counts["completed"] += correlated is not None
        counts["rejected"] += row.get("outcome") == "rejected"
        if submitted is not None:
            activities[(submitted, 0)] += 1
        if correlated is not None:
            activities[(correlated, 1)] -= 1
        samples = by_operation[(command["phase"], command["operation"])]
        if row.get("matcher_ns") is not None:
            samples.append(row["matcher_ns"])
    counts["incomplete"] = counts["submitted"] - counts["completed"]
    outstanding = peak = 0
    for event in sorted(activities):
        outstanding += activities[event]
        peak = max(peak, outstanding)
    require(counts == run["counts"], run_id, f"reported counts differ from measured counts {counts}")
    require(run["peak_outstanding"] == peak, run_id, f"reported outstanding peak differs from {peak}")
    require(run["backlog_drained"] is (outstanding == 0), run_id, "reported backlog drain differs")
    latency = {
        f"{phase}/{operation}": {
            "sample_count": len(samples), "median_ns": median(samples) if samples else None,
        }
        for (phase, operation), samples in sorted(by_operation.items())
    }
    return {"counts": counts, "peak_outstanding": peak, "matcher_latency": latency}


def validate_workloads(trace, regenerated, report):
    traces = validate_traces(trace, regenerated)
    summaries = {}
    observed_scenarios = set()
    comparisons = {}
    for run in report["runs"]:
        name, run_id = run["scenario"], run["run_id"]
        require(run_id not in summaries, run_id, "duplicate workload run ID")
        summaries[run_id] = validate_load_run(run, trace["scenarios"][name])
        observed_scenarios.add(name)
        if comparison := run.get("comparison_id"):
            validate_counts(run["output_counts"], run_id)
            outcomes = [(row["id"], row.get("submitted_ns") is not None, row.get("outcome"))
                        for row in run["requests"]]
            work = (run["trace_id"], outcomes, run["ordered_fills"], run["output_counts"])
            previous = comparisons.setdefault((name, comparison), work)
            require(previous == work, run_id, "compared variants processed different inputs or ordered work")
    require(observed_scenarios == set(SCENARIOS), "paced load", "need a run for each scenario")
    return {"traces": traces, "runs": summaries}


def validate_cache_observations(report, builds):
    recorded = set()
    for observation in report.get("cache_observations", []):
        if observation["status"] == "unavailable":
            identity(observation["reason"], "cache observation reason")
            continue
        require(observation["status"] == "recorded", "cache observation", "unknown status")
        for field in ("method", "event", "filter", "instruction_map", "raw_measurements"):
            identity(observation[field], f"cache observation {field}")
        run_id = identity(observation["run_id"], "cache observation run ID")
        require(run_id not in builds and run_id not in recorded, run_id, "sampling must have a distinct run ID")
        recorded.add(run_id)
        build_id = identity(observation["build_id"], run_id)
        require(builds.get(observation["timing_run_id"]) == build_id,
                run_id, "sampling must name its matching timing run and exact build")
        denominator = positive(observation["denominator"], run_id)
        validate_counts(observation["cache_source_counts"], run_id)
        samples = nonnegative(observation["sample_count"], run_id)
        require(sum(observation["cache_source_counts"].values()) == denominator <= samples,
                run_id, "cache-source counts or denominator disagree")
        nonnegative(observation["lost_samples"], run_id)
    require(recorded or not report.get("cache_source_claims"), "cache observation", "claims require recorded evidence")


def validate_matcher(report):
    require(report["environment"], "matcher timing", "missing environment")
    timing = report["timing"]
    for field in ("start", "stop", "fill_recording"):
        identity(timing[field], f"matcher timing {field}")
    for field in ("event_preparation_included", "notice_preparation_included"):
        require(type(timing[field]) is bool, "matcher timing", f"state whether {field} was included")
    nonnegative(timing["overhead_ns"], "matcher timer overhead")
    require(TIMING_EXCLUSIONS <= set(timing["exclusions"]), "matcher timing", "network or other excluded work included")
    require(report["pairs"], "matcher timing", "no measured pairs")
    pair_orders = defaultdict(list)
    builds = {}
    results = []
    for pair in report["pairs"]:
        trace = identity(pair["trace_id"], "matcher trace ID")
        footprint = positive(pair["working_set_bytes"], "touched footprint")
        case_id = identity(pair["case_id"], "matcher case ID")
        phase = pair["phase"]
        require(phase in ("exploration", "confirmation"), case_id, "unknown experiment phase")
        require(len(pair["runs"]) == 2, case_id, "each pair must contain two runs")
        left, right = pair["runs"]
        require(left["work"] == right["work"], case_id, "ordered fills or per-operation work differ")
        work = left["work"]
        validate_counts(work["operation_counts"], f"{case_id} operations")
        validate_counts(work["output_counts"], f"{case_id} outputs")
        require(isinstance(work["ordered_fills"], list), case_id, "missing ordered fill record")
        require(nonnegative(work["fill_count"], case_id) == len(work["ordered_fills"]),
                case_id, "ordered fill count differs from observed fills")
        nonnegative(work["maker_visits"], case_id)
        nonnegative(work["price_visits"], case_id)
        order = tuple(identity(run["variant"], case_id) for run in pair["runs"])
        require(order[0] != order[1], case_id, "pair needs two distinct variants")
        pair_orders[(case_id, phase, footprint, trace, frozenset(order))].append(order)
        medians = {}
        for run in pair["runs"]:
            run_id = identity(run["run_id"], "matcher run ID")
            require(run_id not in builds, run_id, "duplicate matcher run ID")
            builds[run_id] = identity(run["build_id"], run_id)
            require(run["trace_id"] == trace, run_id, "run trace ID differs from paired trace")
            require(run["timing_only"] is True, run_id, "timing must be separate from heavy observation")
            samples = run["samples_ns"]
            require(samples and run["sample_count"] == len(samples), run_id, "sample count mismatch")
            for sample in samples:
                nonnegative(sample, run_id)
            medians[run["variant"]] = median(samples)
        variants = sorted(medians)
        results.append({
            "case_id": case_id, "working_set_bytes": footprint, "run_order": order, "median_ns": medians,
            "difference_order": variants, "paired_difference_ns": medians[variants[1]] - medians[variants[0]],
            "operations": work["operation_counts"],
            "maker_visits": work["maker_visits"], "price_visits": work["price_visits"],
        })
    for (case_id, *_), orders in pair_orders.items():
        require(len(orders) == 1 or len(set(orders)) == 2, case_id, "repeated pairs did not rotate run order")
    validate_cache_observations(report, builds)
    return {"pairs": results}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="kind", required=True)
    workload = subparsers.add_parser("workload")
    workload.add_argument("--trace", type=Path, required=True)
    workload.add_argument("--regenerated", type=Path, required=True)
    workload.add_argument("--runs", type=Path, required=True)
    matcher = subparsers.add_parser("matcher")
    matcher.add_argument("--runs", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = read_json(args.runs)
        if args.kind == "workload":
            result = validate_workloads(read_json(args.trace), read_json(args.regenerated), report)
        else:
            result = validate_matcher(report)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f"Invalid {args.kind} evidence: {error}\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
