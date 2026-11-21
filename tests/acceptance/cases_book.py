"""Run ordinary order behaviour and Level 3 parity through the exchange's TCP port."""

import random

from . import wire
from .level3 import InvalidMarketData, Level3View, ordered_book, visible_order
from .reference import BUY, SELL, Fill, ReferenceExchange

FIRMS = {
    1: ("FIRM1", "FIRM2"),
    2: ("FIRM1",),
    3: ("FIRM2",),
    4: ("FIRM3",),
}


def equal(actual, expected, meaning):
    if actual != expected:
        raise AssertionError(f"{meaning}: expected {expected!r}, got {actual!r}")


class BookTrace:
    def __init__(self, exchange):
        self.exchange = exchange
        self.reference = ReferenceExchange(FIRMS, (1, 2))
        self.view = Level3View()
        self.subscriber = exchange.subscribe()
        self.sessions = {}
        self.ids = {}
        self.client_ids = {}
        self.client_id = 0
        self.command_sequence = 0
        self.last_updates = []
        equal(exchange.snapshot()["orders"], [], "book starts empty")

    def enter(self, price, quantity, *, access=1, firm="FIRM1", instrument=1, side=SELL):
        expected = self.reference.new_order(access, firm, instrument, side, price, quantity)
        self._run(
            access,
            lambda session, cid: session.new_order(
                cid, instrument, side, price, quantity, firm=firm
            ),
            expected,
            wire.NEW_ORDER,
            quantity=quantity,
        )
        return expected

    def cancel(
        self, order_id, *, access=1, firm="FIRM1", instrument=1, side=SELL, original_id=None
    ):
        expected = self.reference.cancel(access, firm, instrument, side, order_id)
        self._run(
            access,
            lambda session, cid: session.cancel(
                cid,
                self.ids.get(order_id, order_id),
                instrument=instrument,
                side=side,
                firm=firm,
                original_id=original_id,
            ),
            expected,
            wire.CANCEL_REQUEST,
        )
        return expected

    def amend(
        self,
        order_id,
        price,
        total,
        *,
        access=1,
        firm="FIRM1",
        instrument=1,
        side=SELL,
        original_id=None,
        order_type=None,
    ):
        expected = self.reference.amend(
            access, firm, instrument, side, order_id, price, total, order_type
        )
        self._run(
            access,
            lambda session, cid: session.replace(
                cid,
                self.ids.get(order_id, order_id),
                price,
                total,
                instrument=instrument,
                side=side,
                firm=firm,
                original_id=original_id,
                order_type=order_type,
            ),
            expected,
            wire.CANCEL_REPLACE,
            quantity=total,
        )
        return expected

    def _run(self, access, send, expected, request_template, quantity=None):
        if access not in self.sessions:
            self.sessions[access] = self.exchange.connect(access=access)
        session = self.sessions[access]
        self.client_id += 1
        cid = self.client_id
        label = f"request {cid} on access {access} (template {request_template})"
        previously_seen_fills = len(session.fills)
        send(session, cid)
        reply = session.reply(cid)
        for fill in session.fills[previously_seen_fills:]:
            equal(
                fill.fields["client_order_id"], wire.NULL_CLIENT_ORDER_ID, f"{label} fill client ID"
            )
        equal(reply.fields["client_order_id"], cid, f"{label} client order ID echo")
        self._verify_reply(reply, expected, request_template, quantity, label)

        completed = self.exchange.wait_for(
            "command_complete", client_order_id=cid, logical_access_id=access, partition_id=1
        )
        sequence = completed["command_sequence"]
        if sequence <= self.command_sequence:
            raise AssertionError(
                f"{label} command sequence {sequence} followed {self.command_sequence}"
            )
        self.command_sequence = sequence
        if expected.error is not None:
            equal(completed["market_update_count"], 0, f"{label} rejection publishes no changes")
        self._verify_market(completed, expected.fills, label)

    def _verify_reply(self, reply, expected, request_template, quantity, label):
        if expected.error is not None:
            equal(reply.template_id, wire.REJECT, f"{label} rejection for {expected.error}")
            equal(reply.fields["rejected_message_id"], request_template, f"{label} rejected request")
            return

        if request_template == wire.CANCEL_REQUEST:
            equal(reply.template_id, wire.KILL, f"{label} cancellation reply")
            equal(reply.fields["kill_reason"], wire.CLIENT_CANCEL, f"{label} cancellation reason")
        else:
            equal(reply.template_id, wire.ACK, f"{label} acknowledgement")
            ack_type = (
                wire.NEW_ORDER_ACK if request_template == wire.NEW_ORDER else wire.REPLACE_ACK
            )
            equal(reply.fields["ack_type"], ack_type, f"{label} acknowledgement type")
            equal(reply.fields["quantity"], quantity, f"{label} acknowledged total quantity")
            equal(reply.fields["side"], expected.order.side, f"{label} acknowledged side")
            equal(reply.fields["price"], expected.order.price, f"{label} acknowledged price")

        equal(reply.fields["symbol_index"], expected.order.instrument, f"{label} reply instrument")
        order_id = reply.fields["order_id"]
        if request_template == wire.NEW_ORDER:
            if not 0 < order_id < (1 << 64) - 1 or order_id in self.ids.values():
                raise AssertionError(f"{label} received invalid or reused exchange order ID {order_id}")
            self.ids[expected.order.id] = order_id
            self.client_ids[expected.order.id] = self.client_id
        else:
            equal(order_id, self.ids[expected.order.id], f"{label} target exchange order ID")

    def _verify_market(self, completed, fills, label):
        actual = [
            (fill["maker_order_id"], fill["taker_order_id"], fill["price"], fill["quantity"])
            for fill in completed["fills"]
        ]
        expected = [
            (self.ids[fill.maker], self.ids[fill.taker], fill.price, fill.quantity)
            for fill in fills
        ]
        equal(actual, expected, f"{label} ordered fills")
        self.last_updates = self.subscriber.consume(completed["market_update_count"])
        for update in self.last_updates:
            equal(update["command_sequence"], self.command_sequence, f"{label} update sequence")
            self.view.apply(update)

        orders = self.exchange.snapshot()["orders"]
        snapshot = ordered_book(visible_order(order) for order in orders)
        equal(self.view.book, snapshot, f"{label} Level 3 reconstruction versus matcher")
        # The reference owns its own IDs and priority numbering, so compare
        # actual orders in queue order after mapping the IDs from the replies.
        equal(
            [
                (order["instrument"], order["side"], order["price"], order["remaining"], order["order_id"])
                for order in snapshot
            ],
            [
                (order.instrument, order.side, order.price, order.remaining, self.ids[order.id])
                for order in self.reference.book()
            ],
            f"{label} matcher book and time priority versus reference",
        )


def case_price_time_and_instrument(exchange):
    trace = BookTrace(exchange)
    oldest = trace.enter(104, 2).order
    earlier = trace.enter(101, 3).order
    later = trace.enter(101, 2).order
    trace.enter(90, 9, instrument=2)
    take = trace.enter(105, 9, access=2, side=BUY)
    equal(
        take.fills,
        (
            Fill(earlier.id, take.order.id, 101, 3),
            Fill(later.id, take.order.id, 101, 2),
            Fill(oldest.id, take.order.id, 104, 2),
        ),
        "buy fills at the cheapest ask then FIFO, and rests two",
    )
    better_bid = trace.enter(106, 1, side=BUY).order
    trade = trace.enter(100, 2, access=3, firm="FIRM2")
    equal(
        trade.fills,
        (Fill(better_bid.id, trade.order.id, 106, 1), Fill(take.order.id, trade.order.id, 105, 1)),
        "sell fills at the highest bid first, leaving a partial maker",
    )
    trace.enter(106, 2)  # Too expensive to cross the last bid at 105.


def case_fifo_after_cancelling_middle_order(exchange):
    trace = BookTrace(exchange)
    first = trace.enter(100, 2).order
    middle = trace.enter(100, 3).order
    last = trace.enter(100, 5).order
    trace.cancel(middle.id)
    take = trace.enter(101, 4, access=2, side=BUY)
    equal(
        take.fills,
        (Fill(first.id, take.order.id, 100, 2), Fill(last.id, take.order.id, 100, 2)),
        "cancelling the middle order leaves FIFO unchanged",
    )
    fresh = trace.enter(100, 1).order
    equal(
        [(order.id, order.remaining) for order in trace.reference.book()],
        [(last.id, 3), (fresh.id, 1)],
        "remaining queue",
    )


def case_amendment_preserves_priority_after_partial_fill(exchange):
    trace = BookTrace(exchange)
    first = trace.enter(100, 10).order
    second = trace.enter(100, 10).order
    buy = trace.enter(100, 4, access=2, side=BUY)
    equal(buy.fills, (Fill(first.id, buy.order.id, 100, 4),), "initial partial fill")
    smaller = trace.amend(first.id, 100, 8, access=2).order
    equal((smaller.id, smaller.remaining), (first.id, 4), "reduced total after four units traded")
    trace.amend(first.id, 100, 4, access=2)
    trace.amend(first.id, 100, 3, access=2)
    buy = trace.enter(100, 5, access=2, side=BUY)
    equal(
        buy.fills,
        (Fill(first.id, buy.order.id, 100, 4), Fill(second.id, buy.order.id, 100, 1)),
        "only the original reduced remainder trades before the second order",
    )


def case_reprice_and_size_increase_lose_priority(exchange):
    trace = BookTrace(exchange)
    moved = trace.enter(101, 2).order
    increased = trace.enter(100, 1).order
    trace.amend(moved.id, 100, 2)
    equal([order.id for order in trace.reference.book()], [increased.id, moved.id], "repriced FIFO")
    trace.amend(increased.id, 100, 3)
    take = trace.enter(100, 3, access=2, side=BUY)
    equal(
        take.fills,
        (Fill(moved.id, take.order.id, 100, 2), Fill(increased.id, take.order.id, 100, 1)),
        "same-price size increase goes to tail",
    )
    bid = trace.enter(98, 1, access=2, side=BUY).order
    amend = trace.amend(increased.id, 97, 3)
    equal(amend.fills, (Fill(bid.id, increased.id, 98, 1),), "crossing replace at maker price")
    equal(
        (amend.order.id, amend.order.remaining),
        (increased.id, 1),
        "crossing replace keeps ID and cumulative execution",
    )


def case_firm_authorisation_and_order_id_precedence(exchange):
    trace = BookTrace(exchange)
    first = trace.enter(110, 4).order
    other_order = trace.enter(111, 4).order
    other_firm = trace.enter(112, 4, firm="FIRM2").order

    trace.enter(113, 1, access=2, firm="FIRM2")
    trace.amend(other_firm.id, 112, 3, access=2, firm="FIRM2")
    trace.cancel(other_firm.id, access=2, firm="FIRM2")
    trace.cancel(first.id, access=3, firm="FIRM2")
    trace.amend(first.id, 110, 3, side=BUY)
    trace.amend(first.id, 110, 3, order_type=wire.MARKET)
    trace.cancel(900001)
    trace.amend(900001, 109, 1)

    false_original = trace.client_ids[other_order.id]
    trace.amend(None, 109, 1, original_id=false_original)
    trace.cancel(None, original_id=false_original)
    trace.amend(first.id, 110, 3, original_id=false_original)
    trace.amend(first.id, 110, 2, access=2)  # Same firm through another session.
    trace.cancel(first.id, original_id=false_original)
    trace.cancel(first.id)
    trace.amend(first.id, 109, 1)
    trace.cancel(other_firm.id, access=3, firm="FIRM2")
    equal(
        [order.id for order in trace.reference.book()],
        [other_order.id],
        "original client reference cannot target another order",
    )


def case_level3_missing_update_invalidates_view(exchange):
    trace = BookTrace(exchange)
    updates = []
    for price in (101, 102, 103, 104):
        trace.enter(price, 1)
        equal(len(trace.last_updates), 1, "one add per resting order")
        updates.extend(trace.last_updates)

    missing = Level3View()
    missing.apply(updates[0])
    # Withhold sequence 2 before applying sequence 3 to a separate subscriber view.
    for update in (updates[2], updates[3], updates[1]):
        try:
            missing.apply(update)
        except InvalidMarketData:
            equal(missing.valid, False, "view remains invalid after the gap")
        else:
            raise AssertionError(f"invalid view applied sequence {update['sequence']}")
    fresh = Level3View()
    equal(fresh.book, [], "a fresh stream starts with an empty book")
    for update in updates:
        fresh.apply(update)
    equal(fresh.book, trace.view.book, "fresh stream reconstructs the same orders")


def seeded_differential(exchange, seed):
    trace = BookTrace(exchange)
    random_source = random.Random(seed)
    changes = 0
    rejects = 0
    trades = 0
    for step in range(64):
        live = trace.reference.book()
        action = random_source.randrange(8)
        if step % 11 == 0:
            result = trace.enter(100, 1, access=2, firm="FIRM3")
        elif not live or (action < 4 and len(live) < 12):
            access = random_source.choice(tuple(FIRMS))
            result = trace.enter(
                random_source.randint(98, 103),
                random_source.randint(1, 8),
                access=access,
                firm=random_source.choice(FIRMS[access]),
                instrument=random_source.choice((1, 2)),
                side=random_source.choice((BUY, SELL)),
            )
        else:
            order = random_source.choice(live)
            access = random_source.choice([access for access, firms in FIRMS.items() if order.firm in firms])
            route = dict(
                access=access, firm=order.firm, instrument=order.instrument, side=order.side
            )
            if action == 4 or len(live) >= 12:
                result = trace.cancel(order.id, **route)
                changes += 1
            elif action == 5:
                result = trace.amend(order.id, order.price, order.executed, **route)
            else:
                price = order.price + random_source.choice((-2, -1, 1, 2))
                total = order.executed + random_source.randint(1, 8)
                result = trace.amend(order.id, price, total, **route)
                changes += 1
        rejects += result.error is not None
        trades += len(result.fills)
    if not (changes and rejects and trades):
        raise AssertionError(f"seed {seed} did not exercise order changes, rejections and fills")


def case_seeded_differential_1977(exchange):
    seeded_differential(exchange, 1977)


def case_seeded_differential_3851(exchange):
    seeded_differential(exchange, 3851)
