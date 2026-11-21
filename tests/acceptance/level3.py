"""Reconstruct a Level 3 book from deltas and stop on stream loss."""

ORDER_FIELDS = ("instrument", "order_id", "side", "price", "remaining", "priority")


def visible_order(message):
    return {field: message[field] for field in ORDER_FIELDS}


def ordered_book(orders):
    return sorted(
        orders,
        key=lambda order: (
            order["instrument"],
            order["side"],
            -order["price"] if order["side"] == 1 else order["price"],
            order["priority"],
        ),
    )


class InvalidMarketData(ValueError):
    pass


class Level3View:
    def __init__(self):
        self.valid = True
        self.next_sequence = 1
        self._orders = {}

    @property
    def book(self):
        if not self.valid:
            raise InvalidMarketData("book is invalid after a market-data gap")
        return ordered_book(order.copy() for order in self._orders.values())

    def apply(self, update):
        if not self.valid:
            raise InvalidMarketData("start a new book after a market-data gap")

        try:
            sequence = update["sequence"]
            if sequence != self.next_sequence:
                raise InvalidMarketData(
                    f"expected market-data sequence {self.next_sequence}, received {sequence}"
                )
            self._apply(update)
        except KeyError as error:
            self.valid = False
            raise InvalidMarketData(f"update omitted {error.args[0]}") from error
        except InvalidMarketData:
            self.valid = False
            raise
        self.next_sequence += 1

    def _apply(self, update):
        action = update["action"]
        order_id = update["order_id"]
        previous = self._orders.get(order_id)
        if action == "add" and previous is None:
            self._orders[order_id] = visible_order(update)
        elif action == "modify" and previous is not None:
            updated = visible_order(update)
            if (updated["instrument"], updated["side"]) != (
                previous["instrument"], previous["side"]
            ):
                raise InvalidMarketData(f"modify changed identity of order {order_id}")
            self._orders[order_id] = updated
        elif action == "remove" and previous is not None:
            del self._orders[order_id]
        else:
            raise InvalidMarketData(f"unexpected {action!r} update for order {order_id}")
