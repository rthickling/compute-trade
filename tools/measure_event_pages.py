#!/usr/bin/env python3
"""Measure fresh Simplx Event backing during a maximum stalled notice burst."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from acceptance import hardware, wire
from acceptance.driver import launch

SESSIONS = 16
ENTRIES = 64
OLDER_NOTICES = 1
MAX_LIVE = SESSIONS * ENTRIES
NOTICE_LIMIT = MAX_LIVE + OLDER_NOTICES
HANDOFF_LIMIT = 4
BATCH_SIZE = 32
FIRM = "FIRM1"
LIMITS = {
    "live_orders_total": MAX_LIVE,
    "live_orders_per_session": ENTRIES,
    "notice_slots": NOTICE_LIMIT,
    "notice_handoff_slots": HANDOFF_LIMIT,
    "notice_batch_size": BATCH_SIZE,
}
CALL = re.compile(
    r"^\s*([\da-f]+):\s+((?:[\da-f]{2}\s+)+)call\w*\s+.*<__wrap_malloc>$", re.MULTILINE
)
FRESH_BRANCH = "tredzone::AsyncNodesHandle::Shared::WriteCache::newEventPage()"


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def checked_output(*args):
    return subprocess.check_output(args, text=True, env={**os.environ, "LC_ALL": "C"}, timeout=30)


def identify(binary):
    require(re.search(r"Type:\s+EXEC\b", checked_output("readelf", "-h", binary)),
            f"{binary} must be a non-PIE probe build")
    calls = {}
    for address, encoding in CALL.findall(checked_output("objdump", "-dC", binary)):
        start = int(address, 16)
        frames = checked_output("addr2line", "-fCi", "-e", binary, address).splitlines()
        if FRESH_BRANCH in frames:
            return_address = format(start + len(encoding.split()), "x")
            calls[return_address] = {
                "call_address": address, "return_address": return_address, "inline_stack": frames,
            }
    # The pinned engine either inlines the branch into both overloads or emits
    # the branch once as an out-of-line body shared by both callers.
    overloads = {
        item for row in calls.values() for item in row["inline_stack"]
        if item.startswith("tredzone::AsyncNodesHandle::Shared::WriteCache::allocateEvent(")
    }
    out_of_line = any(row["inline_stack"][-2] == FRESH_BRANCH for row in calls.values())
    require(out_of_line or len(overloads) == 2,
            f"could not map the fresh backing allocation for both allocator overloads in {binary}")
    return {
        "binary": str(binary), "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "call_sites": calls,
    }


def environment(identity, trace):
    return {
        **os.environ,
        "CT_EVENT_PAGE_LOG": str(trace),
        "CT_EVENT_PAGE_SITES": ",".join(identity["call_sites"]),
    }


def read_trace(path, identity):
    trace = json.loads(path.read_text())
    require(trace["configured"] and set(trace["sites"]) == set(identity["call_sites"]),
            "probe did not activate the discovered return-address filter")
    require(trace["lost_samples"] == 0, "probe omitted event page allocation records")
    require(all(row["return_address"] in identity["call_sites"]
                and row["bytes"] > 0 and row["cpu"] >= 0 and row["thread_id"] > 0
                and row["monotonic_ns"] > 0 for row in trace["allocations"]),
            "probe delivered an unattributed allocation")
    return trace


def disconnect(exchange, session, access):
    session.close(reset=True)
    return exchange.wait_for("command_sequenced", kind="disconnect", logical_access_id=access,
                             partition_id=1)["command_sequence"]


def add(session, market, client_id):
    session.new_order(client_id, 1, wire.SELL, 100, 1, firm=FIRM)
    ack = session.reply(client_id)
    require(ack.template_id == wire.ACK, f"prefill order {client_id} rejected")
    update = market.read()
    require(update["action"] == "add" and update["order_id"] == ack.fields["order_id"],
            "prefill did not publish its resting order")
    return ack.fields["sequence"], ack.fields["order_id"], update["sequence"]


def wait_stalled(exchange):
    for _ in range(80):
        snapshot = exchange.snapshot()
        if snapshot["notice_handoff_outstanding"] == HANDOFF_LIMIT:
            return snapshot
        time.sleep(0.01)
    raise RuntimeError("notice handoff did not saturate while BlueZone copying was paused")


def verify_replay(exchange, access, last, expected):
    returning = exchange.connect(access=access, partition=1, last_processed=last)
    messages = returning.replay
    require(all(message.template_id == wire.KILL for message in messages)
            and sorted(message.fields["order_id"] for message in messages) == sorted(expected),
            f"session {access} did not replay exactly its removed orders")
    require(all(message.fields["client_order_id"] == wire.NULL_CLIENT_ORDER_ID
                and message.fields["kill_reason"] == wire.DISCONNECT_CANCEL for message in messages),
            f"session {access} got an invalid disconnect notice")
    require([message.fields["sequence"] for message in messages] ==
            list(range(last + 1, last + len(expected) + 1)),
            f"session {access} did not replay in private sequence")
    confirmed = messages[-1].fields["sequence"]
    returning.confirm(confirmed)
    exchange.wait_for("private_confirmed", logical_access_id=access, partition_id=1, sequence=confirmed)


def capture(exchange_binary, identity, raw_trace_path):
    fixtures = {(access, 1): [FIRM] for access in range(1, SESSIONS + 2)}
    with launch(exchange_binary, LIMITS, session_fixtures=fixtures,
                env=environment(identity, raw_trace_path)) as exchange:
        market = exchange.subscribe()
        placement = exchange.control("placement")
        prior_access = SESSIONS + 1
        prior = exchange.connect(access=prior_access)
        prior_reply, prior_id, market_seq = add(prior, market, 1)
        seq = disconnect(exchange, prior, prior_access)
        exchange.wait_for("command_complete", command_sequence=seq)
        prior_remove = market.read()
        require(prior_remove["action"] == "remove" and prior_remove["order_id"] == prior_id
                and prior_remove["sequence"] == market_seq + 1,
                "earlier disconnect did not publish its removal")
        exchange.wait_for("notice_batch_released")
        require(exchange.snapshot()["notice_slots_occupied"] == OLDER_NOTICES,
                "expected one older, unconfirmed notice before the burst")

        clients = {}
        orders = {}
        processed = {}
        market_seq = prior_remove["sequence"]
        for access in range(1, SESSIONS + 1):
            client = exchange.connect(access=access)
            clients[access] = client
            orders[access] = []
            for client_id in range(1, ENTRIES + 1):
                processed[access], order_id, update_sequence = add(client, market, client_id)
                require(update_sequence == market_seq + 1, "prefill market sequence gap")
                market_seq = update_sequence
                orders[access].append(order_id)

        initial = exchange.snapshot()
        require(len(initial["orders"]) == MAX_LIVE and initial["notice_slots_occupied"] == NOTICE_LIMIT,
                "starting book did not fill both the live cap and reservation capacity")
        exchange.control("gate", name="notice_copy", value=False)
        exchange.control("gate", name="command_processing", value=False)
        commands = [(disconnect(exchange, clients[access], access), access) for access in clients]
        queued = exchange.snapshot()["queued_command_sequences"]
        require(queued == [seq for seq, _ in commands], "disconnects were not staged as one burst")

        started = time.monotonic_ns()
        exchange.control("gate", name="command_processing", value=True)
        for seq, access in commands:
            done = exchange.wait_for("command_complete", command_sequence=seq)
            require(done["market_update_count"] == ENTRIES, "disconnect removed the wrong order count")
            removals = market.consume(ENTRIES)
            require(all(row["action"] == "remove" and row["command_sequence"] == seq
                        for row in removals), "publisher delivered an unexpected update")
            require(sorted(row["order_id"] for row in removals) == sorted(orders[access]),
                    "publisher removed the wrong orders")
            require([row["sequence"] for row in removals] ==
                    list(range(market_seq + 1, market_seq + ENTRIES + 1)), "market sequence gap")
            market_seq += ENTRIES

        stalled = wait_stalled(exchange)
        require(not stalled["orders"] and stalled["notice_slots_occupied"] == NOTICE_LIMIT
                and stalled["notice_ready_waiting"] == MAX_LIVE - HANDOFF_LIMIT * BATCH_SIZE,
                "the maximum burst did not remain reserved at bounded handoff capacity")
        stopped = time.monotonic_ns()
        exchange.control("gate", name="notice_copy", value=True)
        for access in clients:
            verify_replay(exchange, access, processed[access], orders[access])
        verify_replay(exchange, prior_access, prior_reply, [prior_id])
        drained = exchange.snapshot()
        require(drained["notice_slots_occupied"] == 0 and not drained["orders"],
                "confirmation did not free every retained notice slot")

    trace = read_trace(raw_trace_path, identity)
    live = [row for row in trace["allocations"] if started <= row["monotonic_ns"] <= stopped]
    # Keep both configured sending cores even if one did not create Events in
    # this scenario. The evidence validator requires an explicit zero count.
    observed_cpus = {worker["cpu"] for worker in placement["workers"]
                     if worker["role"] in ("gateway_matcher", "publisher")}
    require(all(row["cpu"] in observed_cpus for row in live),
            "unexpected worker allocated fresh Event backing in the measured window")
    counts = Counter(row["cpu"] for row in live)
    burst = {
        "reader_delayed": True, "notice_limit": NOTICE_LIMIT,
        "previously_unconfirmed": OLDER_NOTICES, "effective_live_limit": MAX_LIVE,
        "cancelled_orders": MAX_LIVE, "peak_occupied": stalled["notice_slots_peak"],
        "peak_outstanding": stalled["notice_handoff_peak"], "handoff_limit": HANDOFF_LIMIT,
        "ready_waiting_at_peak": stalled["notice_ready_waiting"],
        "probe": "GNU linker wrap=malloc at DWARF-verified fresh Event backing call sites",
        "lost_samples": trace["lost_samples"],
        "fresh_event_pages_by_cpu": {cpu: counts[cpu] for cpu in sorted(observed_cpus)},
    }
    hardware.validate_notice_burst(burst, observed_cpus)
    return {
        "scheduling": "ordinary", "limits": LIMITS, "placement": placement,
        "configuration": "16 live sessions at the global 1,024-order limit; one earlier unconfirmed notice",
        "burst_window_monotonic_ns": [started, stopped], "burst": burst,
        "backing_allocations_outside_burst": len(trace["allocations"]) - len(live),
        "verified_replay_and_confirmations": MAX_LIVE + OLDER_NOTICES,
        "market_removals_checked": MAX_LIVE,
    }


def build_details(build):
    config = {}
    wanted = {"SIMPLX_SOURCE_DIR", "CMAKE_CXX_COMPILER", "CMAKE_BUILD_TYPE"}
    for line in (build / "CMakeCache.txt").read_text().splitlines():
        key = line.partition(":")[0]
        if key in wanted:
            config[key] = line.partition("=")[2]
    dependency = Path(config["SIMPLX_SOURCE_DIR"])
    compile_commands = json.loads((build / "compile_commands.json").read_text())
    return {
        "cmake": config, "simplx_revision": checked_output("git", "-C", dependency, "rev-parse", "HEAD").strip(),
        "simplx_source_changes": checked_output("git", "-C", dependency, "status", "--porcelain").splitlines(),
        "node_compile_command": next(row["command"] for row in compile_commands
                                     if row["file"].endswith("/src/engine/node.cpp")),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new directory for the run evidence")
    args = parser.parse_args()
    build = args.build.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    positive = build / "bin" / "ct_event_page_probe"
    exchange = build / "bin" / "ct_exchange"
    positive_identity, exchange_identity = identify(positive), identify(exchange)
    positive_trace = output / "positive-control-allocations.json"
    subprocess.run([positive], check=True, timeout=10, env=environment(positive_identity, positive_trace))
    control = read_trace(positive_trace, positive_identity)
    require(bool(control["allocations"]), "positive control did not observe any fresh Event backing calls")

    report = {
        "build": build_details(build),
        "positive_control": {**positive_identity, "fresh_backing_allocations": len(control["allocations"]),
                             "queued_events": 4096, "event_page_bytes": 2048},
        "exchange": exchange_identity,
        **capture(exchange, exchange_identity, output / "exchange-allocations.json"),
    }
    result = output / "event-page-report.json"
    result.write_text(json.dumps(report, indent=2) + "\n")
    print(result)


if __name__ == "__main__":
    main()
