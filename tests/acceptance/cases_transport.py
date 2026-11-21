"""Transport, admission, and observable order-path cases."""

import json
import os
from pathlib import Path
import socket
import subprocess
from tempfile import TemporaryDirectory

from . import wire
from .driver import check_fields, read_exact

SESSION = {"logical_access_id": 1, "partition_id": 1}
OTHER = {"logical_access_id": 2, "partition_id": 1}


def completed(exchange, cid, session=SESSION):
    return exchange.wait_for("command_complete", client_order_id=cid, **session)


def rejects(session, cid):
    message = session.reply(cid)
    assert message.template_id == wire.REJECT, message
    return message


def acks(session, cid):
    message = session.reply(cid)
    assert message.template_id == wire.ACK, message
    assert 0 < message.fields["order_id"] < (1 << 64) - 1, message
    return message


def case_fragmented_and_coalesced(exchange):
    exchange.subscribe()
    client = exchange.connect()
    first = client.encode_new_order(101, 1, wire.SELL, 105, 3)
    invalid = client.encode_new_order(102, 1, wire.SELL, 105, 0)
    last = client.encode_new_order(103, 1, wire.SELL, 105, 2)

    offsets = (0, 1, wire.HEADER.size - 1, len(first) - 1)
    for start, end in zip(offsets, offsets[1:]):
        client.send(first[start:end])
        exchange.wait_for("input_buffered", buffered_bytes=end, **SESSION)
        assert exchange.snapshot()["orders"] == []
        client.expect_quiet()

    client.send(first[offsets[-1]:] + invalid + last)
    answers = [acks(client, 101), rejects(client, 102), acks(client, 103)]
    sequences = [answer.fields["sequence"] for answer in answers]
    assert sequences == list(range(sequences[0], sequences[0] + 3)), sequences
    commands = [completed(exchange, cid) for cid in (101, 102, 103)]
    order = [command["command_sequence"] for command in commands]
    assert order == list(range(order[0], order[0] + 3)), order
    assert commands[1]["fills"] == [] and commands[1]["market_update_count"] == 0
    assert {row["remaining"] for row in exchange.snapshot()["orders"]} == {2, 3}


def case_invalid_framing_is_connection_local(exchange):
    exchange.subscribe()
    good = exchange.connect()
    bad = exchange.connect(access=2)
    bad.send(wire.HEADER.pack(0, 0, wire.NEW_ORDER, 0, 354))
    try:
        data = bad.socket.recv(1)
    except ConnectionResetError:
        data = b""
    assert data == b"", data
    good.new_order(104, 1, wire.BUY, 100, 1)
    acks(good, 104)


def case_logon_required(exchange):
    with exchange.raw_connection() as socket_without_logon:
        socket_without_logon.sendall(wire.new_order(105, "FIRM1", 1, wire.BUY, 100, 1))
        try:
            data = socket_without_logon.recv(1)
        except ConnectionResetError:
            data = b""
        assert data == b"", data
    unknown = exchange.connect(access=999, wait=False)
    unknown.expect(wire.LOGON_REJECT)
    assert exchange.snapshot()["orders"] == []


def case_partial_output_retry(exchange):
    exchange.subscribe()
    client = exchange.connect()
    exchange.control("gate", name="private_send_bytes", value=5, **SESSION)
    first = client.encode_new_order(106, 1, wire.SELL, 110, 1)
    second = client.encode_new_order(107, 1, wire.SELL, 110, 1)
    client.send(first + second)
    completed(exchange, 106)
    completed(exchange, 107)
    pending = exchange.wait_for("private_write_blocked", bytes_written=5, **SESSION)
    prefix = read_exact(client.socket, 5)
    client.expect_quiet()
    exchange.control("gate", name="private_send_bytes", value=None, **SESSION)
    first_ack = client.read(prefix=prefix)
    assert first_ack.template_id == wire.ACK, first_ack
    check_fields(first_ack.fields, {"client_order_id": 106})
    second_ack = acks(client, 107)
    assert first_ack.fields["sequence"] == pending["sequence"]
    assert second_ack.fields["sequence"] == pending["sequence"] + 1
    assert first_ack.fields["order_id"] != second_ack.fields["order_id"]
    client.expect_quiet()


def case_queue_full_and_reply_blocked(exchange):
    exchange.subscribe()
    first = exchange.connect()
    second = exchange.connect(access=2)
    exchange.control("gate", name="private_send", value=False, **SESSION)
    first.new_order(110, 1, wire.SELL, 110, 1)
    completed(exchange, 110)
    first.new_order(111, 1, wire.SELL, 105, 1)
    head = exchange.wait_for("command_blocked", reason="reply", client_order_id=111, **SESSION)
    second.new_order(112, 1, wire.SELL, 105, 1)
    queued = exchange.wait_for("command_sequenced", client_order_id=112, **OTHER)
    second.new_order(113, 1, wire.SELL, 105, 1)
    exchange.wait_for("intake_paused", reason="command_queue_full", queue_depth=2)
    waiting = exchange.snapshot()
    assert waiting["queued_command_sequences"] == [
        head["command_sequence"], queued["command_sequence"]
    ], waiting
    assert len(waiting["orders"]) == 1
    second.expect_quiet()

    exchange.control("gate", name="private_send", value=True, **SESSION)
    for client, cid in [(first, 110), (first, 111), (second, 112), (second, 113)]:
        acks(client, cid)
    applied = [completed(exchange, cid, session)["command_sequence"]
               for cid, session in [(111, SESSION), (112, OTHER), (113, OTHER)]]
    assert applied == list(range(applied[0], applied[0] + 3)), applied
    assert exchange.snapshot()["peak_command_queue_depth"] == 2


def case_live_order_capacity(exchange):
    exchange.subscribe()
    first = exchange.connect()
    second = exchange.connect(access=2)
    first.new_order(120, 1, wire.SELL, 110, 1)
    stored = acks(first, 120).fields["order_id"]
    first.new_order(121, 1, wire.SELL, 110, 1)
    rejects(first, 121)
    refused = completed(exchange, 121)
    assert refused["rejection_reason"] == "session_live_order_capacity", refused
    assert refused["market_update_count"] == 0

    second.new_order(122, 1, wire.SELL, 110, 1)
    accepted = acks(second, 122)
    third = exchange.connect(access=3)
    third.new_order(123, 1, wire.SELL, 110, 1, firm="FIRM2")
    rejects(third, 123)
    assert completed(exchange, 123, {"logical_access_id": 3, "partition_id": 1})[
        "rejection_reason"
    ] == "live_order_capacity"
    assert {row["order_id"] for row in exchange.snapshot()["orders"]} == {
        stored, accepted.fields["order_id"]
    }

    first.cancel(124, stored)
    assert first.reply(124).template_id == wire.KILL
    third.new_order(125, 1, wire.SELL, 110, 1, firm="FIRM2")
    acks(third, 125)


def case_price_capacity(exchange):
    exchange.subscribe()
    client = exchange.connect()
    client.new_order(130, 1, wire.SELL, 110, 1)
    resting = acks(client, 130).fields["order_id"]
    client.new_order(131, 1, wire.SELL, 111, 1)
    rejects(client, 131)
    refused = completed(exchange, 131)
    assert refused["rejection_reason"] == "active_price_capacity", refused
    assert refused["market_update_count"] == 0
    assert [row["order_id"] for row in exchange.snapshot()["orders"]] == [resting]

    client.cancel(132, resting)
    assert client.reply(132).template_id == wire.KILL
    client.new_order(133, 1, wire.SELL, 111, 1)
    acks(client, 133)


def case_sweep_capacity_is_atomic(exchange):
    exchange.subscribe()
    seller, buyer = exchange.connect(), exchange.connect(access=2)
    for cid in (140, 141):
        seller.new_order(cid, 1, wire.SELL, 100, 1)
        acks(seller, cid)
    before = exchange.snapshot()["orders"]
    buyer.new_order(142, 1, wire.BUY, 100, 2)
    rejects(buyer, 142)
    refused = completed(exchange, 142, OTHER)
    assert refused["rejection_reason"] == "sweep_capacity", refused
    assert refused["fills"] == [] and refused["market_update_count"] == 0
    assert exchange.snapshot()["orders"] == before

    buyer.new_order(143, 1, wire.BUY, 100, 1)
    acks(buyer, 143)
    assert len(completed(exchange, 143, OTHER)["fills"]) == 1


def worker_for(placement, role):
    matches = [row for row in placement["workers"] if row["role"] == role]
    assert len(matches) == 1, (role, matches)
    return matches[0]


def same_iteration(command, worker):
    stages = command["stages"]
    seen = [stages[phase] for phase in ("frame_complete", "book_applied", "ack_encoded")]
    for sample in seen:
        assert sample["thread_id"] == worker["thread_id"], (sample, worker)
        assert sample["cpu"] == worker["cpu"], (sample, worker)
        assert type(sample["iteration"]) is int and sample["iteration"] >= 0, sample
    assert len({sample["iteration"] for sample in seen}) == 1, seen
    return seen[0]["iteration"]


def case_actual_simplx_iteration(exchange):
    exchange.subscribe()
    placement = exchange.control("placement")
    worker = worker_for(placement, "gateway_matcher")
    seller, buyer = exchange.connect(), exchange.connect(access=2)

    frame = seller.encode_new_order(150, 1, wire.SELL, 100, 2)
    seller.send(frame[:-1])
    incomplete = exchange.wait_for("input_buffered", buffered_bytes=len(frame) - 1, **SESSION)
    seller.send(frame[-1:])
    acks(seller, 150)
    resting = completed(exchange, 150)
    assert resting["fills"] == []
    assert same_iteration(resting, worker) > incomplete["iteration"]

    buyer.new_order(151, 1, wire.BUY, 100, 2)
    acks(buyer, 151)
    full = completed(exchange, 151, OTHER)
    assert [fill["quantity"] for fill in full["fills"]] == [2]
    same_iteration(full, worker)
    assert exchange.snapshot()["orders"] == []

    seller.new_order(152, 1, wire.SELL, 100, 1)
    acks(seller, 152)
    exchange.control("gate", name="private_send", value=False, **OTHER)
    buyer.new_order(153, 1, wire.BUY, 100, 2)
    partial = completed(exchange, 153, OTHER)
    assert [fill["quantity"] for fill in partial["fills"]] == [1]
    assert [row["remaining"] for row in exchange.snapshot()["orders"]] == [1]
    same_iteration(partial, worker)
    buyer.expect_quiet()
    exchange.control("gate", name="private_send", value=True, **OTHER)
    acks(buyer, 153)


def case_private_fills_and_replay(exchange):
    exchange.subscribe()
    first = exchange.connect()
    second = exchange.connect(access=2)
    buyer = exchange.connect(access=3)
    first.new_order(170, 1, wire.SELL, 100, 2)
    second.new_order(171, 1, wire.SELL, 101, 3)
    first_ack, second_ack = acks(first, 170), acks(second, 171)
    execution_enabled, executed = 1 << 6, 1 << 7
    mask = execution_enabled | executed
    for ack in (first_ack, second_ack):
        assert ack.fields["ack_qualifiers"] & mask == execution_enabled, ack

    buyer.new_order(172, 1, wire.BUY, 101, 4, firm="FIRM2")
    buy_ack = buyer.expect(wire.ACK, client_order_id=172, ack_type=wire.NEW_ORDER_ACK, quantity=4)
    assert buy_ack.fields["ack_qualifiers"] & mask == mask, buy_ack
    common = {"client_order_id": wire.NULL_CLIENT_ORDER_ID, "symbol_index": 1}
    for session, ack, price, leaves in [
        (first, first_ack, 100, 0), (second, second_ack, 101, 1)
    ]:
        session.expect(wire.FILL, order_id=ack.fields["order_id"], side=wire.SELL,
                       execution_price=price, execution_quantity=2, leaves_quantity=leaves,
                       sequence=ack.fields["sequence"] + 1, **common)
    bought = []
    for index, (price, leaves) in enumerate([(100, 2), (101, 0)], start=1):
        bought.append(buyer.expect(
            wire.FILL, order_id=buy_ack.fields["order_id"], side=wire.BUY,
            execution_price=price, execution_quantity=2, leaves_quantity=leaves,
            sequence=buy_ack.fields["sequence"] + index, **common
        ))
    buyer.close(reset=True)
    exchange.wait_for("command_complete", kind="disconnect", logical_access_id=3, partition_id=1)
    returning = exchange.connect(access=3, last_processed=buy_ack.fields["sequence"])
    assert returning.replay == bought, (returning.replay, bought)
    assert [row["remaining"] for row in exchange.snapshot()["orders"]] == [1]


def case_injector_reports_incomplete_recovery(exchange):
    exchange.subscribe()
    client = exchange.connect()
    for cid in (180, 181):
        client.new_order(cid, 1, wire.SELL, 100, 1)
    missed, retained = [acks(client, cid) for cid in (180, 181)]
    client.close(reset=True)
    exchange.wait_for("command_complete", kind="disconnect", **SESSION)

    with TemporaryDirectory(prefix="ct-recovery-") as root:
        state = Path(root) / "recovery.json"
        report_path = Path(root) / "report.json"
        state.write_text(json.dumps({"last_processed": 0, "pending_client_order_ids": [180, 181]}))
        result = subprocess.run([
            str(exchange.injector), "--host", "127.0.0.1", "--port", exchange.ports["port"],
            "--logical-access", "1", "--partition", "1", "--firm", "FIRM1",
            "--replay-only", "--recovery-state", str(state), "--report-json", str(report_path),
        ], capture_output=True, text=True, timeout=8, check=False)
        assert report_path.is_file(), (result.returncode, result.stdout, result.stderr)
        report = json.loads(report_path.read_text())
        missing_seq = missed.fields["sequence"]
        check_fields(report, {
            "recovery_complete": False,
            "gaps": [{"first_sequence": missing_seq, "last_sequence": missing_seq}],
            "unknown_client_order_ids": [180],
            "last_processed": retained.fields["sequence"] + 2,
        })
        assert sorted(report["disconnect_order_ids"]) == sorted([
            missed.fields["order_id"], retained.fields["order_id"]
        ]), report


def physical_core(cpu, sysfs="/sys/devices/system/cpu"):
    topology = Path(sysfs) / f"cpu{cpu}" / "topology"
    return ((topology / "physical_package_id").read_text().strip(),
            (topology / "core_id").read_text().strip())


def case_worker_placement_and_event_handoff(exchange):
    subscriber = exchange.subscribe()
    placement = exchange.control("placement")
    matcher = worker_for(placement, "gateway_matcher")
    publisher = worker_for(placement, "publisher")
    core_roles = [matcher, publisher]
    red_cpu_set = {worker["cpu"] for worker in core_roles}
    assert len(red_cpu_set) == 2, core_roles
    for worker in core_roles:
        assert set(os.sched_getaffinity(worker["thread_id"])) == {worker["cpu"]}, worker
    reserved_physical = {physical_core(cpu) for cpu in red_cpu_set}
    assert len(reserved_physical) == 2, core_roles
    for role in ("statistics", "disconnect_replay"):
        worker = worker_for(placement, role)
        cpus = os.sched_getaffinity(worker["thread_id"])
        assert worker["cpu"] in cpus
        assert all(physical_core(cpu) not in reserved_physical for cpu in cpus), worker

    client = exchange.connect()
    client.new_order(160, 1, wire.SELL, 100, 1)
    ack = acks(client, 160)
    command = completed(exchange, 160)
    event = exchange.wait_for("market_event_received", command_sequence=command["command_sequence"])
    assert event["sender_thread_id"] == matcher["thread_id"]
    assert event["sender_cpu"] == matcher["cpu"]
    assert event["receiver_thread_id"] == publisher["thread_id"]
    assert event["receiver_cpu"] == publisher["cpu"]
    message = subscriber.read()
    assert message["sequence"] == 1
    assert message["command_sequence"] == command["command_sequence"]
    assert message["order_id"] == ack.fields["order_id"]


CASES = {
    "fragmented_and_coalesced": (case_fragmented_and_coalesced, {}),
    "invalid_framing_is_connection_local": (case_invalid_framing_is_connection_local, {}),
    "logon_required": (case_logon_required, {}),
    "partial_output_retry": (case_partial_output_retry, {}),
    "queue_full_and_reply_blocked": (case_queue_full_and_reply_blocked, {
        "reply_slots_per_session": 1, "command_queue_capacity": 2,
    }),
    "live_order_capacity": (case_live_order_capacity, {
        "live_orders_per_session": 1, "live_orders_total": 2,
    }),
    "active_price_capacity": (case_price_capacity, {"active_prices": 1}),
    "sweep_capacity_is_atomic": (case_sweep_capacity_is_atomic, {"matching_max_fills": 1}),
    "actual_simplx_iteration": (case_actual_simplx_iteration, {}),
    "private_fills_and_replay": (case_private_fills_and_replay, {}),
    "injector_reports_incomplete_recovery": (case_injector_reports_incomplete_recovery, {
        "ordinary_replay_capacity": 1,
    }),
    "worker_placement_and_event_handoff": (case_worker_placement_and_event_handoff, {}),
}
