"""A small price/time oracle for tests, independent of the exchange and its codecs."""

from dataclasses import dataclass, replace

BUY = 1
SELL = 2
LIMIT = 2


@dataclass(frozen=True)
class Order:
    id: int
    session: int | str
    firm: str
    instrument: int
    side: int
    price: int
    remaining: int
    executed: int
    priority: int


@dataclass(frozen=True)
class Fill:
    maker: int
    taker: int
    price: int
    quantity: int


@dataclass(frozen=True)
class Result:
    order: Order | None = None
    fills: tuple[Fill, ...] = ()
    error: str | None = None


class ReferenceExchange:
    """Match by scanning and sorting a list of live orders for each command."""

    def __init__(self, firms_by_session, instruments):
        self.firms_by_session = {
            session: frozenset(firms) for session, firms in firms_by_session.items()
        }
        self.instruments = frozenset(instruments)
        self.orders: list[Order] = []
        self.next_id = 1
        self.next_priority = 1

    def book(self):
        """Orders in instrument, side, best-price, then time-priority order."""
        return tuple(
            sorted(
                self.orders,
                key=lambda order: (
                    order.instrument,
                    order.side,
                    -order.price if order.side == BUY else order.price,
                    order.priority,
                ),
            )
        )

    def new_order(self, session, firm, instrument, side, price, quantity):
        if firm not in self.firms_by_session.get(session, ()):
            return Result(error="firm_not_authorised")
        if instrument not in self.instruments:
            return Result(error="unknown_instrument")
        if side not in (BUY, SELL) or price <= 0 or quantity <= 0:
            return Result(error="invalid_order")

        order = Order(
            id=self.next_id,
            session=session,
            firm=firm,
            instrument=instrument,
            side=side,
            price=price,
            remaining=quantity,
            executed=0,
            priority=self._fresh_priority(),
        )
        self.next_id += 1
        return self._match(order)

    def cancel(self, session, firm, instrument, side, order_id):
        result = self._find_owned(session, firm, instrument, side, order_id)
        if result.order is not None:
            self.orders.remove(result.order)
        return result

    def amend(self, session, firm, instrument, side, order_id, price, total, order_type=None):
        result = self._find_owned(session, firm, instrument, side, order_id)
        order = result.order
        if order is None:
            return result
        if order_type not in (None, LIMIT):
            return Result(error="incompatible_order")
        if price <= 0 or total <= order.executed:
            return Result(error="invalid_amendment")

        remaining = total - order.executed
        if price == order.price and remaining <= order.remaining:
            updated = replace(order, remaining=remaining)
            self.orders[self.orders.index(order)] = updated
            return Result(order=updated)

        self.orders.remove(order)
        updated = replace(
            order, price=price, remaining=remaining, priority=self._fresh_priority()
        )
        return self._match(updated)

    def disconnect(self, session):
        removed = tuple(order for order in self.orders if order.session == session)
        self.orders = [order for order in self.orders if order.session != session]
        return removed

    def _find_owned(self, session, firm, instrument, side, order_id):
        if firm not in self.firms_by_session.get(session, ()):
            return Result(error="firm_not_authorised")
        order = next((order for order in self.orders if order.id == order_id), None)
        if order is None:
            return Result(error="unknown_order")
        if order.firm != firm:
            return Result(error="wrong_owner")
        if order.instrument != instrument or order.side != side:
            return Result(error="incompatible_order")
        return Result(order=order)

    def _fresh_priority(self):
        priority = self.next_priority
        self.next_priority += 1
        return priority

    def _match(self, incoming):
        resting = [
            maker
            for maker in self.book()
            if maker.instrument == incoming.instrument and maker.side != incoming.side
        ]
        fills = []
        for maker in resting:
            if incoming.remaining == 0:
                break
            crossed = (
                incoming.price >= maker.price
                if incoming.side == BUY
                else incoming.price <= maker.price
            )
            if not crossed:
                break

            fill = min(maker.remaining, incoming.remaining)
            fills.append(Fill(maker.id, incoming.id, maker.price, fill))
            self.orders.remove(maker)
            if maker.remaining > fill:
                self.orders.append(
                    replace(
                        maker,
                        remaining=maker.remaining - fill,
                        executed=maker.executed + fill,
                    )
                )
            incoming = replace(
                incoming,
                remaining=incoming.remaining - fill,
                executed=incoming.executed + fill,
            )

        if incoming.remaining:
            self.orders.append(incoming)
        return Result(order=incoming, fills=tuple(fills))
