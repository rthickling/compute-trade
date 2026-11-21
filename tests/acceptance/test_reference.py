"""Check the independent oracle with small traces whose results are known."""

import unittest

from .level3 import InvalidMarketData, Level3View
from .reference import BUY, SELL, Fill, ReferenceExchange


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.exchange = ReferenceExchange(
            {"A": {"firm1", "firm2"}, "B": {"firm1"}, "C": {"firm2"}}, {1, 2}
        )

    def enter(self, side, price, quantity, session="A", firm="firm1", instrument=1):
        return self.exchange.new_order(session, firm, instrument, side, price, quantity)

    @staticmethod
    def require_order(result):
        assert result.order is not None, result
        return result.order

    def test_price_then_fifo_across_multiple_makers(self):
        self.enter(SELL, 102, 1)  # order 1, oldest but not the best price
        self.enter(SELL, 101, 3)
        self.enter(SELL, 101, 2)
        self.enter(SELL, 100, 1, instrument=2)
        take = self.enter(BUY, 103, 7)

        self.assertEqual(
            take.fills, (Fill(2, 5, 101, 3), Fill(3, 5, 101, 2), Fill(1, 5, 102, 1))
        )
        self.assertEqual([(5, 1), (4, 1)], [(o.id, o.remaining) for o in self.exchange.book()])

    def test_reverse_crossing_chooses_highest_bid_and_rests_uncrossed_order(self):
        self.enter(BUY, 100, 2)
        self.enter(BUY, 101, 3)
        self.assertEqual(self.enter(SELL, 102, 4).fills, ())
        take = self.enter(SELL, 100, 4)

        self.assertEqual(take.fills, (Fill(2, 4, 101, 3), Fill(1, 4, 100, 1)))
        self.assertEqual([(1, 1), (3, 4)], [(o.id, o.remaining) for o in self.exchange.book()])

    def test_partial_execution_reduces_only_open_quantity_and_preserves_priority(self):
        first = self.require_order(self.enter(SELL, 100, 10))
        self.enter(SELL, 100, 10)
        self.enter(BUY, 100, 4)

        amend = self.require_order(self.exchange.amend("B", "firm1", 1, SELL, first.id, 100, 8))
        self.assertEqual((amend.id, amend.remaining, amend.executed), (1, 4, 4))
        self.assertEqual(amend.priority, first.priority)
        before = self.exchange.book()
        for total in (4, 3):
            result = self.exchange.amend("B", "firm1", 1, SELL, first.id, 100, total)
            self.assertEqual(result.error, "invalid_amendment")
            self.assertEqual(self.exchange.book(), before)

        self.assertEqual(self.enter(BUY, 100, 5).fills, (Fill(1, 4, 100, 4), Fill(2, 4, 100, 1)))

    def test_price_change_and_size_increase_lose_priority(self):
        self.enter(SELL, 101, 1)
        self.enter(SELL, 100, 1)
        amend = self.require_order(self.exchange.amend("A", "firm1", 1, SELL, 1, 100, 1))
        self.assertEqual(amend.id, 1)
        self.assertEqual([order.id for order in self.exchange.book()], [2, 1])

        self.exchange.amend("A", "firm1", 1, SELL, 2, 100, 2)
        self.assertEqual([order.id for order in self.exchange.book()], [1, 2])
        self.assertEqual(self.enter(BUY, 100, 2).fills, (Fill(1, 3, 100, 1), Fill(2, 3, 100, 1)))

    def test_crossing_replace_counts_prior_executions_and_keeps_id(self):
        self.enter(SELL, 101, 7)
        self.assertEqual(self.enter(BUY, 101, 2).fills, (Fill(1, 2, 101, 2),))
        self.enter(BUY, 100, 3)

        result = self.exchange.amend("A", "firm1", 1, SELL, 1, 100, 6)
        self.assertEqual(result.fills, (Fill(3, 1, 100, 3),))
        order = self.require_order(result)
        self.assertEqual((order.id, order.executed, order.remaining), (1, 5, 1))

    def test_firm_can_change_orders_through_another_authorised_session(self):
        self.enter(BUY, 100, 2)
        wrong_firm = self.exchange.cancel("C", "firm2", 1, BUY, 1)
        self.assertEqual(wrong_firm.error, "wrong_owner")
        wrong_access = self.exchange.cancel("C", "firm1", 1, BUY, 1)
        self.assertEqual(wrong_access.error, "firm_not_authorised")
        wrong_side = self.exchange.amend("B", "firm1", 1, SELL, 1, 100, 1)
        self.assertEqual(wrong_side.error, "incompatible_order")

        cancelled = self.require_order(self.exchange.cancel("B", "firm1", 1, BUY, 1))
        self.assertEqual(cancelled.id, 1)
        stale = self.exchange.cancel("B", "firm1", 1, BUY, 1)
        self.assertEqual(stale.error, "unknown_order")
        self.assertEqual(self.exchange.book(), ())

    def test_disconnect_is_scoped_to_entry_session_not_firm_or_amending_session(self):
        self.enter(SELL, 101, 3)
        self.enter(SELL, 102, 2, firm="firm2")
        self.enter(SELL, 103, 2, session="B")
        self.enter(BUY, 101, 1, session="B")
        self.exchange.amend("B", "firm1", 1, SELL, 1, 101, 2)

        removed = self.exchange.disconnect("A")
        self.assertEqual(sorted((order.id, order.remaining) for order in removed), [(1, 1), (2, 2)])
        self.assertEqual([(3, 2)], [(order.id, order.remaining) for order in self.exchange.book()])
        self.assertEqual(self.enter(BUY, 103, 2, session="B").fills, (Fill(3, 5, 103, 2),))


class Level3Tests(unittest.TestCase):
    @staticmethod
    def update(sequence, action="add", order_id=10, remaining=3, priority=1):
        return {
            "sequence": sequence, "action": action, "order_id": order_id, "instrument": 1, "side": SELL,
            "price": 100, "remaining": remaining, "priority": priority,
        }

    def test_reconstructs_priority_quantity_and_removal(self):
        book = Level3View()
        self.assertEqual(book.book, [])
        book.apply(self.update(1))
        book.apply(self.update(2, order_id=11, priority=2))
        book.apply(self.update(3, "modify", remaining=1, priority=3))
        self.assertEqual([(o["order_id"], o["remaining"]) for o in book.book], [(11, 3), (10, 1)])
        book.apply(self.update(4, "remove", order_id=11))
        self.assertEqual([o["order_id"] for o in book.book], [10])

    def test_gap_invalidates_view_until_new_stream_starts_from_empty(self):
        book = Level3View()
        book.apply(self.update(1))
        with self.assertRaisesRegex(InvalidMarketData, "expected market-data sequence 2"):
            book.apply(self.update(3, order_id=12))
        self.assertFalse(book.valid)
        with self.assertRaises(InvalidMarketData):
            book.apply(self.update(2, order_id=11))
        with self.assertRaises(InvalidMarketData):
            _ = book.book
        fresh_book = Level3View()
        fresh_book.apply(self.update(1, order_id=21))
        self.assertEqual([o["order_id"] for o in fresh_book.book], [21])

    def test_removal_without_prior_addition_invalidates_view(self):
        book = Level3View()
        with self.assertRaisesRegex(InvalidMarketData, "unexpected 'remove'"):
            book.apply(self.update(1, "remove"))
        self.assertFalse(book.valid)


if __name__ == "__main__":
    unittest.main()
