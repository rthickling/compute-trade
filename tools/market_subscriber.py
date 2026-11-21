"""Subscribe to a new Compute Trade Level 3 stream and rebuild its order book."""

import argparse
import json
import os
import socket
import sys

FIELDS = ("instrument", "order_id", "side", "price", "remaining", "priority")


class Level3:
    def __init__(self):
        self.orders = {}
        self.sequence = 0

    def apply(self, record):
        sequence = record["sequence"]
        if type(sequence) is not int or sequence != self.sequence + 1:
            raise ValueError(f"market sequence gap, expected {self.sequence + 1}, received {sequence!r}")
        order = {name: record[name] for name in FIELDS}
        if any(type(value) is not int for value in order.values()):
            raise ValueError("market order fields must be integers")
        order_id = order["order_id"]
        if order_id <= 0 or order["side"] not in (1, 2):
            raise ValueError("invalid market order identity or side")
        previous = self.orders.get(order_id)
        action = record["action"]
        if action == "add" and previous is None and order["remaining"] > 0:
            self.orders[order_id] = order
        elif action == "modify" and previous is not None and order["remaining"] > 0:
            if (order["instrument"], order["side"]) != (previous["instrument"], previous["side"]):
                raise ValueError("modify changed the instrument or side")
            self.orders[order_id] = order
        elif action == "remove" and previous is not None and order["remaining"] == 0:
            del self.orders[order_id]
        else:
            raise ValueError(f"invalid {action!r} update for order {order_id}")
        self.sequence = sequence

    def snapshot(self):
        return sorted(self.orders.values(),
                      key=lambda row: (row["instrument"], row["side"],
                                       -row["price"] if row["side"] == 1 else row["price"],
                                       row["priority"], row["order_id"]))


def read_record(stream):
    line = stream.readline(513)
    if not line:
        raise EOFError("publisher closed the stream")
    if not line.endswith(b"\n") or len(line) > 512:
        raise ValueError("invalid market record length")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise TypeError("market record must be an object")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--count", type=int, default=0, help="stop after this many updates")
    parser.add_argument("--quiet", action="store_true", help="print the first and final status only")
    args = parser.parse_args()
    if args.count < 0 or not 1 <= args.port <= 65535:
        parser.error("count and port are outside their valid ranges")
    book = Level3()
    try:
        with socket.create_connection((args.host, args.port), timeout=5) as connection:
            connection.sendall(b'{"type":"subscribe"}\n')
            with connection.makefile("rb") as stream:
                if read_record(stream) != {"type": "ready", "sequence": 0}:
                    raise ValueError("publisher did not confirm an empty stream")
                connection.settimeout(None)
                print(json.dumps({
                    "type": "ready", "cpu_affinity": sorted(os.sched_getaffinity(0)),
                }), flush=True)
                while not args.count or book.sequence < args.count:
                    record = read_record(stream)
                    book.apply(record)
                    if not args.quiet:
                        print(json.dumps(record), flush=True)
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, KeyError, EOFError) as error:
        print(f"Market view invalid after sequence {book.sequence}. {error}. Start a new exchange run.",
              file=sys.stderr)
        return 1
    print(json.dumps({"sequence": book.sequence, "orders": book.snapshot()}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
