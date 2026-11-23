"""Transport, admission, and observable order-path cases."""

import itertools
import json
import os
import subprocess
import time
from pathlib import Path
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
    for start, end in itertools.pairwise(offsets):
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


def case_malformed_suffix_follows_complete_frame(exchange):
    market = exchange.subscribe()
    client = exchange.connect()
    good = client.encode_new_order(199, 1, wire.SELL, 100, 1)
    client.send(good + wire.HEADER.pack(0, 0, wire.NEW_ORDER, 0, 354))
    entered = completed(exchange, 199)
    departed = exchange.wait_for("command_complete", kind="disconnect", **SESSION)
    assert departed["command_sequence"] == entered["command_sequence"] + 1
    added, removed = market.consume(2)
    assert (added["action"], removed["action"]) == ("add", "remove")
    assert added["order_id"] == removed["order_id"]


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
    writes = [exchange.wait_for("private_frame_written", sequence=ack.fields["sequence"], **SESSION)
              for ack in (first_ack, second_ack)]
    assert writes[0]["iteration"] == writes[1]["iteration"], writes
    client.expect_quiet()


def case_partial_ack_disconnect_replays_full_frame(exchange):
    exchange.subscribe()
    client = exchange.connect()
    exchange.control("gate", name="private_send_bytes", value=5, **SESSION)
    client.new_order(108, 1, wire.SELL, 110, 1)
    ack = exchange.wait_for("private_reply_ready", client_order_id=108, **SESSION)
    exchange.wait_for("private_write_blocked", sequence=ack["sequence"], bytes_written=5,
                      **SESSION)
    read_exact(client.socket, 5)
    client.close(reset=True)
    exchange.wait_for("command_complete", kind="disconnect", **SESSION)

    exchange.control("gate", name="private_send_bytes", value=None, **SESSION)
    returning = exchange.connect(last_processed=0, wait=False)
    returning.accept_logon()
    reply, notice = returning.finish_logon()
    assert reply.template_id == wire.ACK, reply
    check_fields(reply.fields, {
        "client_order_id": 108, "order_id": ack["order_id"], "sequence": ack["sequence"],
    })
    assert notice.template_id == wire.KILL, notice
    check_fields(notice.fields, {"order_id": ack["order_id"], "sequence": ack["sequence"] + 1})
    assert returning.replay_complete.fields["sequence"] == notice.fields["sequence"]
    returning.new_order(109, 1, wire.SELL, 110, 1)
    assert acks(returning, 109).fields["sequence"] == notice.fields["sequence"] + 1


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
    assert stages["decode_started"] == stages["frame_complete"], stages
    seen = [stages[phase] for phase in ("decode_started", "book_applied", "ack_encoded")]
    for sample in seen:
        assert sample["thread_id"] == worker["thread_id"], (sample, worker)
        assert sample["cpu"] == worker["cpu"], (sample, worker)
        assert type(sample["iteration"]) is int and sample["iteration"] >= 0, sample
    assert len({sample["iteration"] for sample in seen}) == 1, seen


def case_burst_yields_between_callback_turns(exchange):
    exchange.subscribe()
    client = exchange.connect()
    exchange.control("gate", name="command_processing", value=False)
    ids = range(200, 240)
    client.send(b"".join(client.encode_new_order(cid, 1, wire.SELL, 100, 1) for cid in ids))
    last = exchange.wait_for("command_sequenced", client_order_id=239, **SESSION)
    assert len(exchange.snapshot()["queued_command_sequences"]) == len(ids)

    exchange.control("gate", name="command_processing", value=True)
    replies = [acks(client, cid) for cid in ids]
    assert 0 < last["book_in_time"] < (1 << 64) - 1
    assert replies[-1].fields["book_in_time"] == last["book_in_time"]
    applied = [completed(exchange, cid) for cid in ids]
    assert [command["command_sequence"] for command in applied] == list(
        range(last["command_sequence"] - len(ids) + 1, last["command_sequence"] + 1)
    )
    for phase, limit in (("frame_complete", 4), ("book_applied", 32)):
        turns = [command["stages"][phase]["iteration"] for command in applied]
        assert turns == sorted(turns), turns
        assert all(len(list(turn)) <= limit for _, turn in itertools.groupby(turns)), turns


def case_actual_simplx_iteration(exchange):
    exchange.subscribe()
    placement = exchange.control("placement")
    worker = worker_for(placement, "gateway_matcher")
    transport = worker_for(placement, "order_transport")
    transport_identity = {field: transport[field] for field in ("thread_id", "cpu")}
    assert transport["thread_id"] != worker["thread_id"]
    assert transport["cpu"] != worker["cpu"]
    seller, buyer = exchange.connect(), exchange.connect(access=2)
    check_fields(exchange.wait_for("logon_ack_encoded", **SESSION), transport_identity)

    frame = seller.encode_new_order(150, 1, wire.SELL, 100, 2)
    seller.send(frame[:-1])
    incomplete = exchange.wait_for("input_buffered", buffered_bytes=len(frame) - 1, **SESSION)
    check_fields(incomplete, transport_identity)
    assert exchange.snapshot()["orders"] == []
    seller.send(frame[-1:])
    resting_ack = acks(seller, 150)
    resting = completed(exchange, 150)
    assert resting["fills"] == []
    assert incomplete["event_index"] < resting["event_index"]
    same_iteration(resting, worker)
    delivered = exchange.wait_for("private_frame_written", sequence=resting_ack.fields["sequence"],
                                  **SESSION)
    check_fields(delivered, transport_identity)

    buyer.new_order(151, 1, wire.BUY, 100, 2)
    full_ack = acks(buyer, 151)
    buyer.expect(wire.FILL, order_id=full_ack.fields["order_id"],
                 execution_quantity=2, leaves_quantity=0)
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
    blocked = exchange.wait_for("private_write_blocked", bytes_written=0, **OTHER)
    check_fields(blocked, transport_identity)
    buyer.expect_quiet()
    exchange.control("gate", name="private_send", value=True, **OTHER)
    partial_ack = acks(buyer, 153)
    delivered = exchange.wait_for("private_frame_written", sequence=partial_ack.fields["sequence"],
                                  **OTHER)
    check_fields(delivered, transport_identity)
    assert blocked["event_index"] < delivered["event_index"]


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
    assert 0 < buy_ack.fields["book_in_time"] < (1 << 64) - 1, buy_ack
    common = {"client_order_id": wire.NULL_CLIENT_ORDER_ID, "symbol_index": 1}
    sold = []
    for session, ack, price, leaves in [
        (first, first_ack, 100, 0), (second, second_ack, 101, 1)
    ]:
        sold.append(session.expect(
            wire.FILL, order_id=ack.fields["order_id"], side=wire.SELL,
            execution_price=price, execution_quantity=2, leaves_quantity=leaves,
            sequence=ack.fields["sequence"] + 1, **common
        ))
    bought = []
    for index, (price, leaves) in enumerate([(100, 2), (101, 0)], start=1):
        bought.append(buyer.expect(
            wire.FILL, order_id=buy_ack.fields["order_id"], side=wire.BUY,
            execution_price=price, execution_quantity=2, leaves_quantity=leaves,
            sequence=buy_ack.fields["sequence"] + index, **common
        ))
    for maker_fill, taker_fill in zip(sold, bought, strict=True):
        timestamp = maker_fill.fields["trade_time"]
        assert 0 < timestamp < (1 << 64) - 1
        assert timestamp == taker_fill.fields["trade_time"]
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


def case_replay_holds_history_while_other_sessions_trade(exchange):
    exchange.subscribe()
    seller = exchange.connect()
    buyer = exchange.connect(access=2)
    other_seller = exchange.connect(access=3)
    seller.new_order(190, 1, wire.SELL, 100, 1)
    seller.new_order(191, 1, wire.SELL, 101, 1)
    first, second = [acks(seller, cid) for cid in (190, 191)]
    seller.close(reset=True)
    exchange.wait_for("command_complete", kind="disconnect", **SESSION)

    exchange.control("gate", name="replay_send_through", value=first.fields["sequence"], **SESSION)
    returning = exchange.connect(last_processed=0, wait=False)
    returning.accept_logon()
    assert returning.expect(wire.ACK, sequence=first.fields["sequence"]) == first
    returning.new_order(194, 1, wire.SELL, 105, 1)

    buyer.new_order(192, 1, wire.BUY, 100, 1)
    acks(buyer, 192)
    assert completed(exchange, 192, OTHER)["fills"] == []
    # Other sessions can continue matching. The disconnected seller's orders
    # are gone, and its pipelined request waits until replay is finished.
    other_seller.new_order(193, 1, wire.SELL, 100, 1, firm="FIRM2")
    acks(other_seller, 193)
    assert len(completed(exchange, 193, {"logical_access_id": 3, "partition_id": 1})["fills"]) == 1
    returning.expect_quiet()

    exchange.control("gate", name="replay_send_through", value=None, **SESSION)
    replay = returning.finish_logon()
    assert [message.template_id for message in replay] == [wire.ACK, wire.KILL, wire.KILL], replay
    assert replay[0] == second
    assert [message.fields["order_id"] for message in replay[1:]] == [
        first.fields["order_id"], second.fields["order_id"],
    ]
    assert acks(returning, 194).fields["sequence"] == second.fields["sequence"] + 3


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
    blue = worker_for(placement, "order_transport")
    for role in ("statistics", "disconnect_replay", "order_transport"):
        worker = worker_for(placement, role)
        cpus = os.sched_getaffinity(worker["thread_id"])
        assert worker["thread_id"] == blue["thread_id"]
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
    sent = exchange.wait_for("market_frames_sent", sequence=1)
    assert sent["thread_id"] == blue["thread_id"]
    assert sent["cpu"] == blue["cpu"]



def case_publisher_encodes_only_when_an_event_arrives(exchange):
    """An idle publisher does not keep its callback armed."""
    subscriber = exchange.subscribe()
    placement = exchange.control("placement")
    publisher = worker_for(placement, "publisher")
    matcher = worker_for(placement, "gateway_matcher")
    blue = worker_for(placement, "order_transport")
    before = exchange.control("statistics")["publisher_callbacks"]
    assert before >= 1
    time.sleep(0.05)
    assert exchange.control("statistics")["publisher_callbacks"] == before

    session = exchange.connect()
    session.new_order(170, 1, wire.SELL, 100, 1)
    received = exchange.wait_for("market_event_received", command_sequence=1)
    assert received["receiver_thread_id"] == publisher["thread_id"]
    assert received["receiver_cpu"] == publisher["cpu"]
    assert received["sender_thread_id"] == matcher["thread_id"]
    assert received["sender_cpu"] == matcher["cpu"]
    sent = exchange.wait_for("market_frames_sent", sequence=1)
    assert sent["thread_id"] == blue["thread_id"]
    assert sent["cpu"] == blue["cpu"]
    assert subscriber.read()["sequence"] == 1
    after = exchange.control("statistics")["publisher_callbacks"]
    assert before < after <= before + 2
    time.sleep(0.05)
    assert exchange.control("statistics")["publisher_callbacks"] == after
    acks(session, 170)


def case_execution_clock_is_sampled_after_the_match(exchange):
    """book_in_time stays at decode; the fill time is taken when the match commits."""
    exchange.subscribe()
    seller, buyer = exchange.connect(), exchange.connect(access=2)
    seller.new_order(180, 1, wire.SELL, 100, 1)
    acks(seller, 180)
    exchange.control("gate", name="command_processing", value=False)
    buyer.new_order(181, 1, wire.BUY, 100, 1)
    sequenced = exchange.wait_for("command_sequenced", client_order_id=181, **OTHER)
    time.sleep(0.08)
    exchange.control("gate", name="command_processing", value=True)
    done = completed(exchange, 181, OTHER)
    assert done["book_in_time"] == sequenced["book_in_time"]
    acks(buyer, 181)
    fill = buyer.expect(wire.FILL, execution_quantity=1, leaves_quantity=0)
    assert fill.fields["trade_time"] > done["book_in_time"] + 30_000_000


CASES = {
    "fragmented_and_coalesced": (case_fragmented_and_coalesced, {}),
    "invalid_framing_is_connection_local": (case_invalid_framing_is_connection_local, {}),
    "malformed_suffix_follows_complete_frame": (case_malformed_suffix_follows_complete_frame, {}),
    "logon_required": (case_logon_required, {}),
    "partial_output_retry": (case_partial_output_retry, {}),
    "partial_ack_disconnect_replays_full_frame": (
        case_partial_ack_disconnect_replays_full_frame, {"reply_slots_per_session": 1},
    ),
    "queue_full_and_reply_blocked": (case_queue_full_and_reply_blocked, {
        "reply_slots_per_session": 1, "command_queue_capacity": 2,
    }),
    "live_order_capacity": (case_live_order_capacity, {
        "live_orders_per_session": 1, "live_orders_total": 2,
    }),
    "active_price_capacity": (case_price_capacity, {"active_prices": 1}),
    "sweep_capacity_is_atomic": (case_sweep_capacity_is_atomic, {"matching_max_fills": 1}),
    "burst_yields_between_callback_turns": (case_burst_yields_between_callback_turns, {}),
    "actual_simplx_iteration": (case_actual_simplx_iteration, {}),
    "private_fills_and_replay": (case_private_fills_and_replay, {}),
    "injector_reports_incomplete_recovery": (case_injector_reports_incomplete_recovery, {
        "ordinary_replay_capacity": 1,
    }),
    "replay_holds_history_while_other_sessions_trade": (
        case_replay_holds_history_while_other_sessions_trade, {"ordinary_replay_capacity": 2},
    ),
    "worker_placement_and_event_handoff": (case_worker_placement_and_event_handoff, {}),
    "publisher_encodes_only_when_an_event_arrives": (
        case_publisher_encodes_only_when_an_event_arrives, {},
    ),
    "execution_clock_is_sampled_after_the_match": (
        case_execution_clock_is_sampled_after_the_match, {},
    ),
}
