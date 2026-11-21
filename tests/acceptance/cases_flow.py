"""Ordering, reservation and recovery scenarios over the real order-entry socket."""

import time

from . import extensions, wire
from .level3 import Level3View, ordered_book, visible_order


def _equal(actual, expected, context):
    if actual != expected:
        raise AssertionError(f"{context}: expected {expected!r}, got {actual!r}")


def _require(condition, context):
    if not condition:
        raise AssertionError(context)


def _scope(access, partition=1):
    return {"logical_access_id": access, "partition_id": partition}


def _sequenced(exchange, access, client_order_id):
    return exchange.wait_for(
        "command_sequenced", client_order_id=client_order_id, **_scope(access)
    )["command_sequence"]


def _stage_disconnect(exchange, session, access, partition=1):
    session.close(reset=True)
    return exchange.wait_for(
        "command_sequenced", kind="disconnect", **_scope(access, partition)
    )["command_sequence"]


def _completed(exchange, command_sequence):
    return exchange.wait_for("command_complete", command_sequence=command_sequence)


def _ack(session, client_order_id):
    reply = session.reply(client_order_id)
    _equal(reply.template_id, wire.ACK, "private reply template")
    _equal(reply.fields["client_order_id"], client_order_id, "private correlation")
    return reply.fields


def _add(session, client_order_id, side, price, quantity, *, firm="FIRM1"):
    session.new_order(client_order_id, 1, side, price, quantity, firm=firm)
    return _ack(session, client_order_id)


def _fills(command):
    return [
        (fill["maker_order_id"], fill["taker_order_id"], fill["price"], fill["quantity"])
        for fill in command["fills"]
    ]


def _read_updates(subscriber, command):
    updates = subscriber.consume(command["market_update_count"])
    _equal(
        [update["command_sequence"] for update in updates],
        [command["command_sequence"]] * len(updates),
        "publisher command attribution",
    )
    return updates


def _changes(updates):
    return [(update["action"], update["order_id"]) for update in updates]


def _assert_live(exchange, expected):
    orders = exchange.snapshot()["orders"]
    actual = {
        order["order_id"]: (
            order["instrument"], order["side"], order["price"], order["remaining"]
        )
        for order in orders
    }
    _equal(len(actual), len(orders), "each live Order ID occurs once")
    _equal(actual, expected, "matcher book")


def _kill_notices(messages, expected_order_ids):
    notices = [message.fields for message in messages if message.template_id == wire.KILL]
    _equal(
        sorted(notice["order_id"] for notice in notices),
        sorted(expected_order_ids),
        "one disconnect notice per affected order",
    )
    for notice in notices:
        _equal(notice["kill_reason"], wire.DISCONNECT_CANCEL, "Optiq disconnect Kill reason")
        _equal(
            notice["client_order_id"], wire.NULL_CLIENT_ORDER_ID,
            "automatic Kill has null Client Order ID",
        )
    return notices


def _assert_increasing_private_sequences(messages):
    sequences = [
        message.fields["sequence"]
        for message in messages
        if message.template_id != extensions.REPLAY_GAP
    ]
    _equal(sequences, sorted(set(sequences)), "replayed private sequence order")


def publication_overtake(exchange):
    """A later order that fits must wait behind one requiring more output credits."""
    subscriber = exchange.subscribe()
    seller = exchange.connect(access=4)
    first = exchange.connect(access=2)
    second = exchange.connect(access=3)
    maker = _add(seller, 1, wire.SELL, 100, 1, firm="FIRM3")["order_id"]
    _equal(_changes(subscriber.consume(1)), [("add", maker)], "resting maker")

    # Market data publishes one removal for the fill, plus an add if any quantity rests.
    exchange.control("gate", name="publication_credits", value=1)
    first.new_order(2, 1, wire.BUY, 100, 2)
    first_sequence = _sequenced(exchange, 2, 2)
    exchange.wait_for(
        "command_blocked",
        command_sequence=first_sequence,
        reason="publication",
        required_updates=2,
    )
    second.new_order(3, 1, wire.BUY, 100, 1, firm="FIRM2")
    second_sequence = _sequenced(exchange, 3, 3)
    _equal(second_sequence, first_sequence + 1, "cross-session command order")
    blocked = exchange.snapshot()
    _equal(
        blocked["queued_command_sequences"],
        [first_sequence, second_sequence],
        "both commands wait despite space for the later order",
    )
    _require(blocked["command_queue_entry_size"] > 0, "missing command queue entry size")
    _require(blocked["peak_command_queue_depth"] >= 2, "queue peak did not reach two")
    _assert_live(exchange, {maker: (1, wire.SELL, 100, 1)})

    exchange.control("gate", name="publication_credits", value=None)
    first_id = _ack(first, 2)["order_id"]
    second_id = _ack(second, 3)["order_id"]
    first_command = _completed(exchange, first_sequence)
    second_command = _completed(exchange, second_sequence)
    _equal(_fills(first_command), [(maker, first_id, 100, 1)], "older buyer gets fill")
    _equal(_fills(second_command), [], "later buyer cannot consume the same maker")
    first_updates = _read_updates(subscriber, first_command)
    second_updates = _read_updates(subscriber, second_command)
    _equal(
        _changes(first_updates),
        [("remove", maker), ("add", first_id)],
        "first buyer publication order",
    )
    _equal(
        _changes(second_updates),
        [("add", second_id)],
        "second buyer publishes later",
    )
    _require(first_updates[1]["priority"] < second_updates[0]["priority"], "resting FIFO order")
    _assert_live(exchange, {first_id: (1, wire.BUY, 100, 1), second_id: (1, wire.BUY, 100, 1)})


def disconnect_command_order(exchange):
    """The disconnect removes precisely the orders entered on its session."""
    subscriber = exchange.subscribe()
    departing = exchange.connect(access=1)
    same_firm = exchange.connect(access=2)
    before = exchange.connect(access=3)
    after = exchange.connect(access=4)
    partial = _add(departing, 10, wire.SELL, 100, 10)
    unfilled = _add(departing, 11, wire.SELL, 101, 5, firm="FIRM2")
    other = _add(same_firm, 12, wire.SELL, 102, 5)["order_id"]
    affected = [partial["order_id"], unfilled["order_id"]]
    initial_updates = subscriber.consume(3)
    _equal(
        _changes(initial_updates),
        [("add", order_id) for order_id in affected + [other]],
        "starting market book",
    )
    same_firm.replace(15, partial["order_id"], 100, 9)
    amendment = _ack(same_firm, 15)
    _equal(amendment["ack_type"], wire.REPLACE_ACK, "amend from another authorised session")
    _equal(amendment["order_id"], partial["order_id"], "amend keeps exchange Order ID")
    amend_updates = subscriber.consume(1)
    _equal(_changes(amend_updates), [("modify", partial["order_id"])], "amend update")

    exchange.control("gate", name="command_processing", value=False)
    before.new_order(13, 1, wire.BUY, 100, 3, firm="FIRM2")
    before_sequence = _sequenced(exchange, 3, 13)
    disconnect = _stage_disconnect(exchange, departing, 1)
    after.new_order(14, 1, wire.BUY, 102, 2, firm="FIRM3")
    after_sequence = _sequenced(exchange, 4, 14)
    _equal(
        exchange.snapshot()["queued_command_sequences"],
        [before_sequence, disconnect, after_sequence],
        "earlier crossing, disconnect and later crossing are queued in order",
    )
    exchange.control("gate", name="command_processing", value=True)

    before_id = _ack(before, 13)["order_id"]
    after_id = _ack(after, 14)["order_id"]
    early_command = _completed(exchange, before_sequence)
    disconnect_command = _completed(exchange, disconnect)
    late_command = _completed(exchange, after_sequence)
    _equal(
        _fills(early_command),
        [(partial["order_id"], before_id, 100, 3)],
        "fill sequenced before the disconnect",
    )
    _equal(_fills(disconnect_command), [], "disconnect only removes resting quantity")
    _equal(
        _fills(late_command), [(other, after_id, 102, 2)], "fill sequenced after disconnect"
    )
    early_updates = _read_updates(subscriber, early_command)
    _equal(_changes(early_updates), [("modify", partial["order_id"])], "earlier fill update")
    _equal(early_updates[0]["remaining"], 6, "earlier fill leaves six of the amended quantity")
    disconnect_updates = _read_updates(subscriber, disconnect_command)
    _equal(
        sorted(_changes(disconnect_updates)),
        sorted(("remove", order_id) for order_id in affected),
        "subscriber sees both departing firms' removals",
    )
    late_updates = _read_updates(subscriber, late_command)
    _equal(_changes(late_updates), [("modify", other)], "same firm on other session remains")
    _equal(late_updates[0]["remaining"], 3, "remaining quantity on surviving session")
    _assert_live(exchange, {other: (1, wire.SELL, 102, 3)})
    view = Level3View()
    for updates in (initial_updates, amend_updates, early_updates, disconnect_updates, late_updates):
        for update in updates:
            view.apply(update)
    _equal(
        view.book,
        ordered_book(visible_order(order) for order in exchange.snapshot()["orders"]),
        "subscriber reconstructs the surviving book after the disconnect",
    )

    other_partition = exchange.connect(access=1, partition=2)
    _equal(other_partition.replay, [], "same Logical Access on another partition sees no notices")
    reconnect = exchange.connect(access=1, last_processed=unfilled["sequence"])
    _assert_increasing_private_sequences(reconnect.replay)
    _require(
        all(message.template_id in (wire.FILL, wire.KILL) for message in reconnect.replay),
        "replay after both entry acknowledgements contains an unexpected private message",
    )
    notices = _kill_notices(reconnect.replay, affected)
    fills = [message.fields for message in reconnect.replay if message.template_id == wire.FILL]
    _require(len(fills) <= 1, "departing session cannot receive a duplicate or unrelated fill")
    if fills:
        [fill] = fills
        _equal(
            fill["client_order_id"], wire.NULL_CLIENT_ORDER_ID, "Fill client reference is null"
        )
        _equal(fill["order_id"], partial["order_id"], "private Fill identifies the departing maker")
        _equal(fill["execution_quantity"], 3, "replayed maker Fill quantity")
        _equal(fill["execution_price"], 100, "replayed maker Fill price")
        _require(fill["sequence"] < notices[0]["sequence"], "earlier Fill follows the disconnect")


def _stalled_handoff(exchange, waiting):
    # The matcher may complete the disconnect just before it gets a chance to offer a batch.
    deadline = time.monotonic() + 3
    while True:
        snapshot = exchange.snapshot()
        handoff_outstanding = snapshot["notice_handoff_outstanding"]
        if handoff_outstanding == 1 and snapshot["notice_ready_waiting"] == waiting:
            return snapshot
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"notice handoff did not stall with {waiting} waiting: {snapshot!r}"
            )
        time.sleep(0.01)


def notice_capacity(exchange):
    """Reservations bound live orders plus every unconfirmed disconnect record."""
    exchange.subscribe()
    old = exchange.connect(access=1, partition=2)
    old_order = _add(old, 20, wire.SELL, 100, 1)
    _completed(exchange, _stage_disconnect(exchange, old, 1, partition=2))
    exchange.wait_for("notice_batch_released")
    _equal(exchange.snapshot()["notice_slots_occupied"], 1, "older unconfirmed notice")

    first = exchange.connect(access=1)
    second = exchange.connect(access=2)
    third = exchange.connect(access=3)
    idle = exchange.connect(access=4)
    first_orders = [
        _add(first, 21, wire.SELL, 101, 2),
        _add(first, 22, wire.SELL, 102, 1, firm="FIRM2"),
    ]
    second_order = _add(second, 23, wire.SELL, 103, 1)
    third_order = _add(third, 24, wire.SELL, 104, 1, firm="FIRM2")
    _equal(exchange.snapshot()["notice_slots_occupied"], 5, "prior notice plus four reservations")

    exchange.control("gate", name="notice_copy", value=False)
    exchange.control("gate", name="command_processing", value=False)
    disconnects = [
        _stage_disconnect(exchange, session, access)
        for access, session in [(1, first), (2, second), (3, third)]
    ]
    _equal(
        exchange.snapshot()["queued_command_sequences"],
        disconnects,
        "three disconnects wait in gateway order",
    )
    exchange.control("gate", name="command_processing", value=True)
    for sequence in disconnects:
        _equal(_fills(_completed(exchange, sequence)), [], "disconnect cannot trade")
    _assert_live(exchange, {})

    stalled = _stalled_handoff(exchange, waiting=3)
    _equal(stalled["notice_slots_occupied"], 5, "no slots freed by the disconnect handoff")
    _equal(stalled["notice_slots_peak"], 5, "peak combined reservations and notices")
    _equal(stalled["notice_handoff_outstanding"], 1, "single handoff slot occupied")
    _equal(stalled["notice_handoff_peak"], 1, "handoff limit observed during burst")
    _equal(stalled["notice_ready_waiting"], 3, "additional notices wait in reserved storage")
    [batch_id] = stalled["notice_batches_in_flight"]

    idle.new_order(25, 1, wire.SELL, 105, 1, firm="FIRM3")
    rejected = idle.reply(25)
    _equal(rejected.template_id, wire.REJECT, "empty session rejected at global notice capacity")
    _equal(rejected.fields["client_order_id"], 25, "capacity Reject preserves request reference")
    command = _completed(exchange, _sequenced(exchange, 4, 25))
    _equal(command["rejection_reason"], "notice_capacity", "global notice limit caused rejection")
    _equal(_fills(command), [], "capacity Reject creates no fills")
    _equal(command["market_update_count"], 0, "capacity Reject creates no publication")
    _assert_live(exchange, {})
    _equal(
        exchange.snapshot()["notice_batches_in_flight"], [batch_id],
        "the sender could not reuse its slot while the consumer was paused",
    )
    _equal(exchange.snapshot()["notice_slots_occupied"], 5, "Reject changes no reservations")

    exchange.control("gate", name="notice_copy", value=True)
    copied = exchange.wait_for("notice_batch_copied", batch_id=batch_id)
    released = exchange.wait_for("notice_batch_released", batch_id=batch_id)
    _require(copied["event_index"] < released["event_index"], "handoff reused before copy")
    for access, orders in [(1, first_orders), (2, [second_order]), (3, [third_order])]:
        reconnect = exchange.connect(access=access, last_processed=orders[-1]["sequence"])
        _assert_increasing_private_sequences(reconnect.replay)
        _equal(
            [message.template_id for message in reconnect.replay],
            [wire.KILL] * len(orders),
            "replay contains only the unconfirmed notices after the entry acknowledgements",
        )
        _kill_notices(reconnect.replay, [order["order_id"] for order in orders])

    prior = exchange.connect(access=1, partition=2, last_processed=old_order["sequence"])
    [earlier_notice] = _kill_notices(prior.replay, [old_order["order_id"]])
    _equal(exchange.snapshot()["notice_slots_occupied"], 5, "replay does not confirm processing")
    prior.confirm(earlier_notice["sequence"])
    exchange.wait_for("private_confirmed", sequence=earlier_notice["sequence"], **_scope(1, 2))
    _equal(exchange.snapshot()["notice_slots_occupied"], 4, "one confirmation frees one slot")
    retry = _add(idle, 26, wire.SELL, 105, 1, firm="FIRM3")
    _assert_live(exchange, {retry["order_id"]: (1, wire.SELL, 105, 1)})
    _equal(exchange.snapshot()["notice_slots_occupied"], 5, "retry reserves exactly one slot")


def notice_reservation_release(exchange):
    """A full fill and a requested cancel each release a live order's slot."""
    exchange.subscribe()
    maker = exchange.connect(access=1)
    taker = exchange.connect(access=3)
    resting_id = _add(maker, 50, wire.SELL, 100, 2)["order_id"]
    _equal(exchange.snapshot()["notice_slots_occupied"], 1, "single reservation filled")

    taker_id = _add(taker, 51, wire.BUY, 100, 2, firm="FIRM2")["order_id"]
    crossed = _completed(exchange, _sequenced(exchange, 3, 51))
    _equal(_fills(crossed), [(resting_id, taker_id, 100, 2)], "crossing needs no new reservation")
    _equal(exchange.snapshot()["notice_slots_occupied"], 0, "full fill releases reservation")
    _assert_live(exchange, {})

    cancelled_id = _add(taker, 52, wire.SELL, 101, 1, firm="FIRM2")["order_id"]
    _equal(exchange.snapshot()["notice_slots_occupied"], 1, "new order reclaims the slot")
    taker.cancel(53, cancelled_id, firm="FIRM2")
    cancelled = taker.reply(53)
    _equal(cancelled.template_id, wire.KILL, "requested cancellation succeeds")
    _equal(cancelled.fields["order_id"], cancelled_id, "requested cancellation removes that order")
    _equal(exchange.snapshot()["notice_slots_occupied"], 0, "cancel releases reservation")
    _assert_live(exchange, {})


def _withhold_ack_then_disconnect(exchange, *, hold_notice_copy):
    exchange.subscribe()
    session = exchange.connect(access=1)
    if hold_notice_copy:
        exchange.control("gate", name="notice_copy", value=False)
    exchange.control("gate", name="private_send", value=False, **_scope(1))
    session.new_order(30, 1, wire.SELL, 100, 2)
    prepared = exchange.wait_for(
        "private_reply_ready", client_order_id=30, template_id=wire.ACK, **_scope(1)
    )
    _completed(exchange, _stage_disconnect(exchange, session, 1))
    exchange.control("gate", name="private_send", value=True, **_scope(1))
    return prepared


def _replayed_ack_and_notice(messages, prepared):
    _equal(
        [message.template_id for message in messages],
        [wire.ACK, wire.KILL],
        "missed acknowledgement replays before its disconnect notice",
    )
    ack = messages[0].fields
    _equal(ack["client_order_id"], 30, "replay restores Client Order ID pairing")
    _equal(ack["order_id"], prepared["order_id"], "replay restores exchange Order ID pairing")
    _equal(ack["sequence"], prepared["sequence"], "replay preserves acknowledgement sequence")
    [notice] = _kill_notices(messages, [prepared["order_id"]])
    _equal(notice["sequence"], ack["sequence"] + 1, "subsequent notice sequence")
    return notice["sequence"]


def pending_notice_replay(exchange):
    """Reconnect while the notice is in flight and pipeline a fresh order."""
    prepared = _withhold_ack_then_disconnect(exchange, hold_notice_copy=True)
    _stalled_handoff(exchange, waiting=0)
    returning = exchange.connect(access=1, last_processed=0, wait=False)
    # The fixture already knows request sequence 1 was processed. Pipeline its
    # successor before receiving Logon Ack, which might wait for notice copy.
    returning.next_request_sequence = 2
    returning.new_order(31, 1, wire.SELL, 101, 1)
    exchange.wait_for("replay_requested", last_processed=0, resuming=True, **_scope(1))

    exchange.control("gate", name="notice_copy", value=True)
    replay = returning.finish_logon()
    notice_sequence = _replayed_ack_and_notice(replay, prepared)
    _equal(returning.replay_complete.fields["sequence"], notice_sequence, "replay frontier")
    replay_completed = exchange.wait_for("replay_completed", sequence=notice_sequence, **_scope(1))
    new_order = _ack(returning, 31)
    _equal(new_order["sequence"], notice_sequence + 1, "new reply is sequenced after replay")
    command = _completed(exchange, _sequenced(exchange, 1, 31))
    _require(
        replay_completed["event_index"] < command["event_index"],
        "matching processed the pipelined order before replay finished",
    )
    _require(new_order["order_id"] != prepared["order_id"], "a cancelled Order ID was reused")
    _assert_live(exchange, {new_order["order_id"]: (1, wire.SELL, 101, 1)})


def interrupted_replay(exchange):
    """The client resumes from its last fully processed private message."""
    prepared = _withhold_ack_then_disconnect(exchange, hold_notice_copy=False)
    exchange.control(
        "gate", name="replay_send_through", value=prepared["sequence"], **_scope(1)
    )
    interruption = exchange.connect(access=1, last_processed=0, wait=False)
    interruption.accept_logon()
    interruption.expect(
        wire.ACK, client_order_id=30, order_id=prepared["order_id"], sequence=prepared["sequence"]
    )
    # This is a second, distinct connection close on the same configured session.
    interruption.close(reset=True)
    exchange.control("gate", name="replay_send_through", value=None, **_scope(1))

    retry = exchange.connect(access=1, last_processed=prepared["sequence"])
    _equal(
        [message.template_id for message in retry.replay], [wire.KILL],
        "the fully processed acknowledgement is not replayed again",
    )
    [notice] = _kill_notices(retry.replay, [prepared["order_id"]])
    _equal(notice["sequence"], prepared["sequence"] + 1, "retry starts at the next sequence")
    retry.confirm(notice["sequence"])
    exchange.wait_for("private_confirmed", sequence=notice["sequence"], **_scope(1))
    _equal(exchange.snapshot()["notice_slots_occupied"], 0, "confirmation frees notice slot")


def ordinary_reply_gap(exchange):
    """Eviction reports the exact missing sequence and keeps disconnect notices."""
    exchange.subscribe()
    session = exchange.connect(access=1)
    exchange.control("gate", name="private_send", value=False, **_scope(1))
    session.new_order(40, 1, wire.SELL, 100, 2)
    lost = exchange.wait_for(
        "private_reply_ready", client_order_id=40, template_id=wire.ACK, **_scope(1)
    )
    session.new_order(41, 1, wire.SELL, 101, 3)
    retained = exchange.wait_for(
        "private_reply_ready", client_order_id=41, template_id=wire.ACK, **_scope(1)
    )
    _equal(retained["sequence"], lost["sequence"] + 1, "consecutive ordinary replies")
    _completed(exchange, _stage_disconnect(exchange, session, 1))
    exchange.control("gate", name="private_send", value=True, **_scope(1))

    returning = exchange.connect(access=1, last_processed=0)
    _equal(
        [message.template_id for message in returning.replay],
        [extensions.REPLAY_GAP, wire.ACK, wire.KILL, wire.KILL],
        "gap report, retained reply and both unconfirmed notices arrive in order",
    )
    gap, ack, *notices = returning.replay
    _equal(gap.fields["first_sequence"], lost["sequence"], "start of missing range")
    _equal(gap.fields["last_sequence"], lost["sequence"], "end of missing range")
    _equal(ack.fields["client_order_id"], 41, "only the retained request gains a replayed pairing")
    _equal(ack.fields["order_id"], retained["order_id"], "retained order ID pairing")
    _kill_notices(notices, [lost["order_id"], retained["order_id"]])
    _assert_increasing_private_sequences(returning.replay)

    # Account for the gap and acknowledge the watermark up to the notices that arrived.
    last = notices[-1].fields["sequence"]
    _equal(returning.replay_complete.fields["sequence"], last, "gap recovery frontier")
    returning.confirm(last)
    exchange.wait_for("private_confirmed", sequence=last, **_scope(1))
    _equal(exchange.snapshot()["notice_slots_occupied"], 0, "confirming after a gap frees notices")
    returning.close(reset=True)
    recovered = exchange.connect(access=1, last_processed=last)
    _equal(recovered.replay, [], "no stale gap or notices on the next reconnect")
    _add(recovered, 42, wire.SELL, 103, 1)


CASES = {
    "publication_overtake": (publication_overtake, {}),
    "disconnect_command_order": (disconnect_command_order, {}),
    "notice_capacity": (
        notice_capacity,
        {
            "notice_slots": 5,
            "notice_handoff_slots": 1,
            "notice_batch_size": 1,
            "live_orders_per_session": 3,
        },
    ),
    "pending_notice_replay": (pending_notice_replay, {}),
    "interrupted_replay": (interrupted_replay, {}),
    "ordinary_reply_gap": (ordinary_reply_gap, {"ordinary_replay_capacity": 1}),
    "notice_reservation_release": (notice_reservation_release, {"notice_slots": 1}),
}
