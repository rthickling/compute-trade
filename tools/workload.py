#!/usr/bin/env python3
"""Summarise the published AAPL LOBSTER sample and generate synthetic traces."""

import argparse
import csv
import hashlib
import io
import json
import random
import sys
from collections import Counter
from math import ceil
from pathlib import Path
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from acceptance import evidence, reference

STOCK = "AAPL"
SAMPLE_DATE = "2012-06-21"
WINDOW = (36000, 37800)
SOURCE = "https://php.lobsterdata.com/info/DataSamples.php"
PRICE_MULTIPLIER = 10000
EQUITY_TICK = 100
MID = 10_000
BAND = 3
FIRM = "FIRM1"
BUY, SELL = reference.BUY, reference.SELL


def quantiles(values):
    items = sorted(values)
    if not items:
        return {}
    return {
        str(percent): items[max(0, ceil(len(items) * percent / 100) - 1)]
        for percent in (50, 90, 95, 99, 100)
    }


def csv_member(archive, suffix):
    names = [name for name in archive.namelist() if name.endswith(suffix)]
    if len(names) != 1:
        raise ValueError(f"expected one {suffix} file in sample archive")
    return io.TextIOWrapper(archive.open(names[0]), encoding="ascii", newline="")


def summarize(path):
    event_count = Counter()
    requests_per_second = Counter()
    requests_per_10ms = Counter()
    add_sizes = []
    visible_trade_sizes = []
    occupancy = {BUY: [], SELL: []}
    best_quantity = {BUY: [], SELL: []}
    quantity_in_three_ticks = {BUY: [], SELL: []}
    spreads, midpoint_moves = [], []
    previous_mid = None

    with (
        ZipFile(path) as archive,
        csv_member(archive, "_message_10.csv") as source_messages,
        csv_member(archive, "_orderbook_10.csv") as source_book,
    ):
        messages, books = csv.reader(source_messages), csv.reader(source_book)
        for message, row in zip(messages, books, strict=True):
            whole, _, fraction = message[0].partition(".")
            second = int(whole)
            if second >= WINDOW[1]:
                break
            if second < WINDOW[0]:
                continue
            kind, size = int(message[1]), int(message[3])
            event_count[kind] += 1
            if kind in (1, 2, 3):
                requests_per_second[second] += 1
                requests_per_10ms[second * 100 + int((fraction + "00")[:2])] += 1
            if kind == 1:
                add_sizes.append(size)
            elif kind == 4:
                visible_trade_sizes.append(size)
            values = list(map(int, row))
            levels = {
                SELL: [(p, q) for p, q in zip(values[0::4], values[1::4]) if q],
                BUY: [(p, q) for p, q in zip(values[2::4], values[3::4]) if q],
            }
            if not levels[BUY] or not levels[SELL]:
                continue
            bid, ask = levels[BUY][0][0], levels[SELL][0][0]
            mid = bid + ask
            spreads.append(ask - bid)
            if previous_mid is not None and previous_mid != mid:
                midpoint_moves.append(abs(mid - previous_mid))
            previous_mid = mid
            for side, side_levels in levels.items():
                best_price, size = side_levels[0]
                occupancy[side].append(len(side_levels))
                best_quantity[side].append(size)
                depth = sum(q for p, q in side_levels if abs(p - best_price) <= 3 * EQUITY_TICK)
                quantity_in_three_ticks[side].append(depth)

    if not add_sizes or not spreads:
        raise ValueError("sample lacks the expected AAPL observations in the selected window")
    return {
        "venue": "NASDAQ", "symbol": STOCK, "date": SAMPLE_DATE,
        "timezone": "America/New_York", "window_local": ["10:00:00", "10:30:00"],
        "feed": "LOBSTER ten-level sample derived from NASDAQ Historical TotalView-ITCH",
        "source": SOURCE,
        "archive_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "price_multiplier": PRICE_MULTIPLIER,
        "total_book_event_rows": sum(event_count.values()),
        "event_counts": dict(sorted(event_count.items())),
        "add_shares": quantiles(add_sizes),
        "visible_execution_shares": quantiles(visible_trade_sizes),
        "spread_price_units": quantiles(spreads),
        "nonzero_midpoint_change_twice_price_units": quantiles(midpoint_moves),
        "observed_add_cancel_delete_per_second": quantiles(
            requests_per_second[i] for i in range(*WINDOW)
        ),
        "observed_add_cancel_delete_per_10ms": quantiles(
            requests_per_10ms[i] for i in range(WINDOW[0] * 100, WINDOW[1] * 100)
        ),
        "displayed_shares_at_best": {side: quantiles(xs) for side, xs in best_quantity.items()},
        "displayed_shares_within_three_ticks_of_best": {
            side: quantiles(xs) for side, xs in quantity_in_three_ticks.items()
        },
        "occupied_prices_within_ten_reported_levels": {
            side: quantiles(xs) for side, xs in occupancy.items()
        },
    }


def book_summary(book):
    rows = {}
    for side in (BUY, SELL):
        orders = [(row["price"], row["quantity"]) for row in book if row["side"] == side]
        prices = {price for price, _ in orders}
        rows[str(side)] = {
            "orders": len(orders), "levels": len(prices),
            "price_range": [min(prices), max(prices)],
        }
    return rows


class Trace:
    def __init__(self, units, spread, scale, rate, burst, seed):
        self.book = reference.ReferenceExchange({1: [FIRM]}, [1])
        self.starting = []
        self.commands = []
        self.observations = []
        self.spread = spread
        self.rng = random.Random(seed)
        self.units = units
        self.rate = rate
        self.burst = burst
        self.seed = seed
        for side in (BUY, SELL):
            for level in range(5):
                price = MID - spread - level if side == BUY else MID + spread + level
                for slot in range(3):
                    quantity = scale * (units + (level + slot) % 3)
                    order = self.book.new_order(1, FIRM, 1, side, price, quantity).order
                    assert order is not None
                    self.starting.append({
                        "order_id": order.id, "instrument": 1, "side": side,
                        "price": price, "quantity": quantity,
                    })

    def command(self, phase, operation, side, price=None, quantity=None, target=None):
        index = len(self.commands)
        # Natural bursts in the observed trace motivate a small group at each
        # scheduled time. Keep the long-run rate independent of burst size.
        row = {
            "id": 100_001 + index, "planned_ns": (index // self.burst) * self.burst * 10**9 // self.rate,
            "phase": phase, "operation": operation, "instrument": 1, "side": side,
        }
        if operation == "new":
            result = self.book.new_order(1, FIRM, 1, side, price, quantity)
        elif operation == "cancel":
            result = self.book.cancel(1, FIRM, 1, side, target)
            row["target_order_id"] = target
        else:
            result = self.book.amend(1, FIRM, 1, side, target, price, quantity)
            row["target_order_id"] = target
        if result.error:
            raise ValueError(f"generator produced a rejected command: {row!r} {result.error}")
        assert result.order is not None
        if operation == "new":
            row["order_id"] = result.order.id
        if operation != "cancel":
            row["price"], row["quantity"] = price, quantity

        bids = [order for order in self.book.book() if order.side == BUY]
        asks = [order for order in self.book.book() if order.side == SELL]
        best_bid = bids[0].price if bids else None
        fills = [
            {"maker_order_id": fill.maker, "taker_order_id": fill.taker, "price": fill.price,
             "quantity": fill.quantity}
            for fill in result.fills
        ]
        self.commands.append(row)
        self.observations.append({
            "id": row["id"], "best_bid": best_bid,
            "best_ask": asks[0].price if asks else None,
            "available_buy_quantity": (
                sum(order.remaining for order in bids if order.price >= best_bid - BAND)
                if best_bid is not None else 0
            ),
            "fills": fills, "prices_crossed": list(dict.fromkeys(fill["price"] for fill in fills)),
        })
        return result.order.id

    def quiet(self, rounds, *, phase="quiet", stable_orders=False):
        for index in range(rounds):
            side = self.rng.choice((BUY, SELL))
            price = MID - self.spread - 1 if side == BUY else MID + self.spread + 1
            quantity = self.units + self.rng.randint(0, 3)
            identity = self.command(phase, "new", side, price, quantity)
            self.command(phase, "replace", side, price, quantity - 1, identity)
            self.command(phase, "cancel", side, target=identity)
            if index % 4 == 0:
                # Cross one unit and add that unit back at the former maker price.
                maker = next(order for order in self.book.book() if order.side != side)
                self.command(phase, "new", side, maker.price, 1)
                if stable_orders and maker.remaining > 1:
                    # Keep the number of makers stable over a long trace.
                    self.command(phase, "replace", maker.side, maker.price,
                                 maker.executed + maker.remaining + 1, maker.id)
                else:
                    self.command(phase, "new", maker.side, maker.price, 1)

    def result(self):
        return {
            "seed": self.seed, "instrument": 1, "initial_book": self.starting,
            "initial_book_summary": book_summary(self.starting),
            "commands": self.commands, "observations": self.observations,
        }


def generate(summary, rounds, seed, shock_count, shock_size, rate_override, burst_override,
             duration=None):
    units = max(3, min(20, summary["add_shares"]["50"] // 20))
    spread = max(1, min(4, ceil(summary["spread_price_units"]["50"] / (2 * EQUITY_TICK))))
    request_median = summary["observed_add_cancel_delete_per_second"]["50"]
    rate = rate_override or max(4, min(100, request_median))
    burst = burst_override or max(1, min(4, summary["observed_add_cancel_delete_per_10ms"]["99"]))
    if duration:
        planned = duration * rate
        if not 16 <= planned <= 25_000:
            raise ValueError("duration and rate must plan 16..25000 commands per scenario")
        # A quiet round averages 3.5 commands. Leave complete rounds intact.
        rounds = ceil(planned / 3.5)
    traces = {}
    for name in evidence.SCENARIOS:
        scale = 1 if name.startswith("thin") else 4
        trace = Trace(units, spread, scale, rate, burst, seed)
        if name.endswith("quiet"):
            trace.quiet(rounds, stable_orders=duration is not None)
        else:
            before_shock = max(1, rounds * 3 // 4)
            trace.quiet(before_shock, stable_orders=duration is not None)
            sell = shock_size or (units + 1) * 5
            for _ in range(shock_count):
                trace.command("shock", "new", SELL, MID - spread - 4, sell)
            for offset in range(5):
                trace.command("replenishment", "new", BUY, MID - spread - offset, sell)
            if duration:
                trace.quiet(rounds - before_shock, phase="replenishment", stable_orders=True)
        traces[name] = trace.result()
    result = {
        "near_best_ticks": BAND,
        "calibration": {
            "source_sha256": summary["archive_sha256"], "order_quantity_unit": units,
            "half_spread_ticks": spread, "offered_requests_per_second": rate,
            "burst_requests": burst, "deep_quantity_multiplier": 4, "quiet_rounds": rounds,
            "shock_requests": shock_count, "shock_units_per_request": shock_size or (units + 1) * 5,
        },
        "scenarios": traces,
    }
    if duration:
        result["calibration"]["duration_seconds_target"] = duration
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    summary = commands.add_parser("summarize")
    summary.add_argument("--sample", type=Path, required=True)
    summary.add_argument("--output", type=Path, required=True)
    trace = commands.add_parser("generate")
    trace.add_argument("--summary", type=Path, required=True)
    trace.add_argument("--output", type=Path, required=True)
    length = trace.add_mutually_exclusive_group()
    length.add_argument("--rounds", type=int, default=48)
    length.add_argument("--duration-seconds", type=int,
                        help="target duration of each scenario, with a bounded maker count")
    trace.add_argument("--seed", type=int, default=354)
    trace.add_argument("--shock-count", type=int, default=4)
    trace.add_argument("--shock-size", type=int, help="override units per shock sell")
    trace.add_argument("--rate", type=int, help="override the calibrated requests per second")
    trace.add_argument("--burst", type=int, help="override the calibrated requests per scheduled time")
    args = parser.parse_args()
    if args.action == "summarize":
        result = summarize(args.sample)
    else:
        if not 4 <= args.rounds <= 150:
            parser.error("--rounds must be in 4..150")
        if not 1 <= args.shock_count <= 16 or (
            args.shock_size is not None and not 1 <= args.shock_size <= 1024
        ):
            parser.error("shock-count must be 1..16 and shock-size must be 1..1024")
        if (args.rate is not None and not 1 <= args.rate <= 100_000) or (
            args.burst is not None and not 1 <= args.burst <= 64
        ):
            parser.error("rate must be 1..100000 and burst must be 1..64")
        if args.duration_seconds is not None and not 1 <= args.duration_seconds <= 600:
            parser.error("duration-seconds must be 1..600")
        try:
            result = generate(json.loads(args.summary.read_text()), args.rounds, args.seed,
                              args.shock_count, args.shock_size, args.rate, args.burst,
                              args.duration_seconds)
        except ValueError as error:
            parser.error(str(error))
        evidence.validate_traces(result, result)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
