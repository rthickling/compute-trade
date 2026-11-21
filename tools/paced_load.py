#!/usr/bin/env python3
"""Replay the four frozen exchange workloads over the normal binary TCP entry point."""

import argparse
import hashlib
import json
import os
import select
import socket
import sys
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from queue import Full, Queue
from threading import Condition, Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from acceptance import evidence, wire
from acceptance.driver import launch
from acceptance.level3 import Level3View, ordered_book, visible_order

NS = 10**9
FIRM = "FIRM1"


@contextmanager
def housekeeping(placement):
    original = os.sched_getaffinity(0)

    def physical_core(cpu):
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        return (
            (topology / "physical_package_id").read_text(),
            (topology / "core_id").read_text(),
        )

    reserved = {
        physical_core(worker["cpu"])
        for worker in placement["workers"]
        if worker["role"] in ("gateway_matcher", "publisher")
    }
    available = {cpu for cpu in original if physical_core(cpu) not in reserved}
    if not available:
        raise RuntimeError(
            "no housekeeping CPU remains for the paced client and subscriber"
        )
    os.sched_setaffinity(0, available)
    try:
        yield sorted(available)
    finally:
        os.sched_setaffinity(0, original)


class MarketDrain:
    def __init__(self, subscriber):
        self.subscriber = subscriber
        self.condition = Condition()
        self.view = Level3View()
        self.counts = Counter()
        self.error = None
        self.stopping = False
        self.thread = Thread(target=self.run, name="market-subscriber", daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopping = True
        try:
            self.subscriber.socket.shutdown(socket.SHUT_RD)
        except OSError:
            pass
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError("market subscriber did not stop")

    def run(self):
        try:
            while not self.stopping:
                update = self.subscriber.read()
                with self.condition:
                    self.view.apply(update)
                    self.counts[update["command_sequence"]] += 1
                    self.condition.notify_all()
        except (OSError, EOFError, ValueError) as error:
            if not self.stopping:
                with self.condition:
                    self.error = error
                    self.condition.notify_all()

    def wait_until(self, count, timeout=3):
        with self.condition:
            reached = self.condition.wait_for(
                lambda: self.error is not None or sum(self.counts.values()) >= count,
                timeout,
            )
            if self.error:
                raise RuntimeError(f"invalid market data: {self.error}") from self.error
            return reached


class CommandDrain:
    """Collect completion diagnostics before the exchange can evict them."""

    def __init__(self, exchange):
        self.exchange = exchange
        self.pending = Queue(maxsize=1024)
        self.records = {}
        self.error = None
        self.thread = Thread(target=self.run, name="command-diagnostics", daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        if self.thread.is_alive():
            try:
                self.pending.put(None, timeout=1)
            except Full:
                self.error = "command diagnostics backlog exceeded its safety limit"
            self.thread.join(timeout=6)
        if self.thread.is_alive() and self.error is None:
            self.error = "command diagnostics did not drain"

    def offer(self, cid):
        if self.error:
            return
        try:
            self.pending.put_nowait(cid)
        except Full:
            self.error = "command diagnostics backlog exceeded its safety limit"

    def run(self):
        cid = None
        try:
            while (cid := self.pending.get()) is not None:
                self.records[cid] = self.exchange.wait_for(
                    "command_complete",
                    logical_access_id=1,
                    partition_id=1,
                    client_order_id=cid,
                )
        except (AssertionError, EOFError, OSError, ValueError) as error:
            self.error = f"cannot collect command {cid}: {error}"


def encode(command, order_id, next_sequence):
    cid = command["id"]
    instrument, side = command["instrument"], command["side"]
    operation = command["operation"]
    if operation == "new":
        return wire.new_order(
            cid,
            FIRM,
            instrument,
            side,
            command["price"],
            command["quantity"],
            sequence=next_sequence,
        )
    if operation == "cancel":
        return wire.cancel(
            cid, FIRM, instrument, side, order_id, sequence=next_sequence
        )
    return wire.replace(
        cid,
        FIRM,
        instrument,
        side,
        order_id,
        command["price"],
        command["quantity"],
        sequence=next_sequence,
    )


def decode_frames(buffer):
    while len(buffer) >= wire.HEADER.size:
        frame_size, *_ = wire.HEADER.unpack_from(buffer)
        if frame_size < wire.HEADER.size or frame_size > 256:
            raise ValueError(f"invalid reply frame length {frame_size}")
        if len(buffer) < frame_size:
            break
        message = wire.decode_frame(buffer[:frame_size])
        del buffer[:frame_size]
        yield message


def prefill(session, rows):
    mapping = {}
    for client_id, order in enumerate(rows, 1):
        session.new_order(
            client_id,
            order["instrument"],
            order["side"],
            order["price"],
            order["quantity"],
        )
        response = session.reply(client_id)
        if response.template_id != wire.ACK:
            raise ValueError(f"exchange refused starting-order {order}: {response}")
        mapping[order["order_id"]] = response.fields["order_id"]
    return mapping


def pace(session, commands, order_ids, window, drain_ns, on_reply=None):
    sock = session.socket
    sock.setblocking(False)
    origin = time.monotonic_ns()
    deadline = commands[-1]["planned_ns"] + drain_ns
    rows = [{"id": command["id"], "send_attempts_ns": []} for command in commands]
    pending = {}
    all_fills = []
    blocked_ids = set()
    receive_buffer = bytearray()
    frame = None
    sent = 0
    index = 0
    next_sequence = session.next_request_sequence
    observed_failure = None

    def now():
        return time.monotonic_ns() - origin

    try:
        while (index < len(commands) or pending) and now() < deadline:
            next_command = commands[index] if index < len(commands) else None
            if (
                frame is None
                and next_command is not None
                and now() >= next_command["planned_ns"]
                and len(pending) < window
            ):
                target = next_command.get("target_order_id")
                if target is None or target in order_ids:
                    frame = encode(next_command, order_ids.get(target), next_sequence)
                    sent = 0
                    rows[index]["encoded_ns"] = now()
                elif target in blocked_ids:
                    rows[index]["dependency"] = "prior_order_rejected"
                    index += 1
                    continue
                else:
                    rows[index]["dependency"] = "waiting_for_exchange_order_id"
            if frame is not None:
                assert next_command is not None
                attempt = now()
                if attempt >= deadline:
                    break
                row = rows[index]
                original = row["send_attempts_ns"]
                original.append(attempt)
                if len(original) == 1:
                    row["lateness_ns"] = attempt - next_command["planned_ns"]
                try:
                    count = sock.send(memoryview(frame)[sent:])
                except BlockingIOError:
                    count = None
                if count == 0:
                    raise OSError("connection closed during request submission")
                if count is not None:
                    sent += count
                    if sent == len(frame):
                        row["submitted_ns"] = now()
                        pending[next_command["id"]] = (next_command, row)
                        next_sequence += 1
                        frame = None
                        index += 1

            # Always give TCP input a turn. There is no wait for an Ack before
            # offering independent due requests unless the client window is full.
            next_command = commands[index] if index < len(commands) else None
            time_left = max(0, deadline - now())
            target = (
                next_command.get("target_order_id")
                if next_command is not None
                else None
            )
            eligible = (
                next_command is not None
                and len(pending) < window
                and (target is None or target in order_ids or target in blocked_ids)
            )
            if frame is not None:
                timeout = time_left / NS
            elif next_command is not None and eligible:
                timeout = (
                    max(0, min(time_left, next_command["planned_ns"] - now())) / NS
                )
            else:
                timeout = time_left / NS
            if not next_command and not pending:
                break
            ready, _, _ = select.select(
                [sock], [sock] if frame is not None else [], [], timeout
            )
            if not ready:
                continue
            chunk = sock.recv(16384)
            if not chunk:
                raise OSError("connection closed while draining replies")
            receive_buffer.extend(chunk)
            for reply in decode_frames(receive_buffer):
                decoded = now()
                if reply.template_id == wire.FILL:
                    all_fills.append(reply.fields)
                    continue
                if reply.template_id not in (wire.ACK, wire.KILL, wire.REJECT):
                    raise ValueError(
                        f"unexpected order-entry message {reply.template_id}"
                    )
                cid = reply.fields["client_order_id"]
                if cid not in pending:
                    raise ValueError(f"unexpected reply for Client Order ID {cid}")
                command, row = pending.pop(cid)
                if reply.template_id == wire.ACK and command["operation"] == "new":
                    order_ids[command["order_id"]] = reply.fields["order_id"]
                elif reply.template_id == wire.REJECT and command["operation"] == "new":
                    blocked_ids.add(command["order_id"])
                row["reply_decoded_ns"] = decoded
                row["outcome"] = (
                    "rejected" if reply.template_id == wire.REJECT else "accepted"
                )
                correlated = now()
                row["reply_correlated_ns"] = correlated
                row["rtt_ns"] = correlated - row["send_attempts_ns"][0]
                if on_reply:
                    on_reply(cid)
    except OSError as error:
        observed_failure = str(error)

    # Fills may follow the final Ack. Drain any remaining client bytes outside
    # the measured command timestamps and before inspecting exchange evidence.
    drain_end = time.monotonic() + 0.02
    while not pending and not observed_failure and time.monotonic() < drain_end:
        ready, _, _ = select.select(
            [sock], [], [], max(0, drain_end - time.monotonic())
        )
        if not ready:
            break
        chunk = sock.recv(16384)
        if not chunk:
            break
        receive_buffer.extend(chunk)
        for reply in decode_frames(receive_buffer):
            if reply.template_id != wire.FILL:
                raise ValueError(f"late private message {reply.template_id}")
            all_fills.append(reply.fields)

    return rows, all_fills, now(), observed_failure


def accounting(rows):
    actions = Counter()
    counts = {
        "planned": len(rows),
        "attempted": sum(bool(row["send_attempts_ns"]) for row in rows),
        "submitted": sum("submitted_ns" in row for row in rows),
        "completed": sum("reply_correlated_ns" in row for row in rows),
        "rejected": sum(row.get("outcome") == "rejected" for row in rows),
    }
    counts["incomplete"] = counts["submitted"] - counts["completed"]
    for row in rows:
        if "submitted_ns" in row:
            actions[(row["submitted_ns"], 0)] += 1
        if "reply_correlated_ns" in row:
            actions[(row["reply_correlated_ns"], 1)] -= 1
    outstanding = peak = 0
    for timestamp in sorted(actions):
        outstanding += actions[timestamp]
        peak = max(peak, outstanding)
    return counts, peak


def command_diagnostics(exchange, requests, captured=None):
    recorded = []
    for row in requests:
        if "reply_correlated_ns" not in row:
            continue
        if captured is None:
            completed = exchange.wait_for(
                "command_complete",
                logical_access_id=1,
                partition_id=1,
                client_order_id=row["id"],
            )
        else:
            completed = captured.get(row["id"])
            if completed is None:
                continue
        if "matcher_ns" in completed:
            row["matcher_ns"] = completed["matcher_ns"]
        recorded.append(completed)
    return recorded


def run_case(executable, scenario, name, run_id, build_id, window, drain_ns):
    with launch(executable) as exchange:
        placement = exchange.control("placement")
        subscriber = exchange.subscribe()
        subscriber.socket.settimeout(
            max(6, scenario["commands"][-1]["planned_ns"] / NS + 5)
        )
        with housekeeping(placement) as client_cpus, MarketDrain(subscriber) as market:
            session = exchange.connect()
            ids = prefill(session, scenario["initial_book"])
            if len(scenario["commands"]) > 400:
                with CommandDrain(exchange) as captured:
                    rows, private_fills, drain_until, failure = pace(
                        session,
                        scenario["commands"],
                        ids,
                        window,
                        drain_ns,
                        captured.offer,
                    )
                records = command_diagnostics(exchange, rows, captured.records)
                diagnostic_error = captured.error
            else:
                rows, private_fills, drain_until, failure = pace(
                    session, scenario["commands"], ids, window, drain_ns
                )
                records = command_diagnostics(exchange, rows)
                diagnostic_error = None
            published = sum(row["market_update_count"] for row in records)
            market_complete = market.wait_until(
                len(scenario["initial_book"]) + published
            )
            actual = [fill for row in records for fill in row["fills"]]
            expected = [
                {
                    **item,
                    "maker_order_id": ids.get(item["maker_order_id"]),
                    "taker_order_id": ids.get(item["taker_order_id"]),
                }
                for observation in scenario["observations"]
                for item in observation["fills"]
            ]
            counts, peak = accounting(rows)
            telemetry_complete = (
                len(records) == counts["completed"] and diagnostic_error is None
            )
            parity = (
                actual == expected
                and counts["completed"] == counts["planned"]
                and telemetry_complete
            )
            gauges = exchange.snapshot()
            with market.condition:
                market_parity = (
                    telemetry_complete
                    and market_complete
                    and market.view.book
                    == ordered_book(visible_order(order) for order in gauges["orders"])
                )
            result = {
                "run_id": run_id,
                "build_id": build_id,
                "scenario": name,
                "trace_id": evidence.trace_id(scenario),
                "market_data_enabled": True,
                "requests": rows,
                "counts": counts,
                "peak_outstanding": peak,
                "backlog_drained": counts["incomplete"] == 0,
                "drain_until_ns": drain_until,
                "ordered_fills": actual,
                "ordered_fills_match_reference": parity,
                "market_updates_drained": market_complete,
                "market_book_matches_exchange": market_parity,
                "output_counts": {
                    "market_updates": published,
                    "private_fills_read": len(private_fills),
                },
                "queue_peak": gauges["peak_command_queue_depth"],
                "queue_entry_size": gauges["command_queue_entry_size"],
                "worker_placement": placement["workers"],
                "client_and_subscriber_cpus": client_cpus,
                "command_diagnostics": "streamed"
                if len(scenario["commands"]) > 400
                else "after_load",
                "command_diagnostics_complete": telemetry_complete,
            }
            if diagnostic_error:
                result["diagnostics_error"] = diagnostic_error
            if failure:
                result["transport_error"] = failure
            return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exchange", required=True, type=Path)
    parser.add_argument("--trace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", choices=evidence.SCENARIOS, action="append")
    parser.add_argument("--window", type=int, default=64)
    parser.add_argument("--drain-seconds", type=float, default=3)
    args = parser.parse_args()
    if not 1 <= args.window <= 1024 or not 0 < args.drain_seconds <= 60:
        parser.error(
            "--window must be 1..1024 and --drain-seconds must be above zero and at most 60"
        )
    executable = args.exchange.resolve()
    build_id = hashlib.sha256(executable.read_bytes()).hexdigest()
    trace = json.loads(args.trace.read_text())
    evidence.validate_traces(trace, trace)
    report = {"runs": []}
    for name in args.scenario or evidence.SCENARIOS:
        if hashlib.sha256(executable.read_bytes()).hexdigest() != build_id:
            parser.exit(1, "exchange executable changed between scenarios\n")
        scenario = trace["scenarios"][name]
        run = run_case(
            executable,
            scenario,
            name,
            f"{args.run_id}/{name}",
            build_id,
            args.window,
            int(args.drain_seconds * NS),
        )
        if hashlib.sha256(executable.read_bytes()).hexdigest() != build_id:
            parser.exit(1, f"exchange executable changed during {name}\n")
        evidence.validate_load_run(run, scenario)
        report["runs"].append(run)
        args.output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
        print(
            name,
            run["counts"],
            "fills_match=",
            run["ordered_fills_match_reference"],
            "published=",
            run["market_updates_drained"],
            flush=True,
        )
    return int(
        any(
            run["counts"]["completed"] != run["counts"]["planned"]
            or run["counts"]["rejected"]
            or not run["ordered_fills_match_reference"]
            or not run["market_book_matches_exchange"]
            for run in report["runs"]
        )
    )


if __name__ == "__main__":
    sys.exit(main())
