#!/usr/bin/env python3
"""Exercise the real TCP actor path using a codec independent of the C++ codec."""

import os
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time


SOFH = struct.Struct("!IH")
SBE_HEADER = struct.Struct("<HHHH")
ORDER = struct.Struct("<QIBqQ")
ACK = struct.Struct("<QQIBBQQ")
ENCODING = 0xEB50
TIMEOUT = 5


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def order(client_id, instrument=1, side=1, price=100, quantity=10,
          block=29, template=1, schema=1, version=0):
    body = ORDER.pack(client_id, instrument, side, price, quantity)
    payload = SBE_HEADER.pack(block, template, schema, version) + body
    return SOFH.pack(SOFH.size + len(payload), ENCODING) + payload


def receive_exact(connection, length):
    result = bytearray()
    while len(result) < length:
        data = connection.recv(length - len(result))
        if not data:
            raise AssertionError("connection closed before a complete acknowledgement")
        result.extend(data)
    return bytes(result)


def acknowledgement(connection):
    length, encoding = SOFH.unpack(receive_exact(connection, SOFH.size))
    check(encoding == ENCODING, "incorrect acknowledgement SOFH encoding")
    check(length == 52, "incorrect acknowledgement frame length")
    payload = receive_exact(connection, length - SOFH.size)
    header = SBE_HEADER.unpack_from(payload)
    check(header == (38, 2, 1, 0), f"incorrect acknowledgement SBE header: {header}")
    values = ACK.unpack_from(payload, SBE_HEADER.size)
    return dict(zip(("client_id", "order_id", "instrument", "status", "reason",
                     "executed", "remaining"), values))


def accepted(connection, client_id, executed, remaining, instrument=1):
    response = acknowledgement(connection)
    check(response["status"] == 0 and response["reason"] == 0,
          f"expected accepted order: {response}")
    check(response["client_id"] == client_id and response["instrument"] == instrument,
          f"acknowledgement identifies the wrong order: {response}")
    check(0 < response["order_id"] < (1 << 64) - 1,
          f"accepted order has no valid exchange order id: {response}")
    check(response["executed"] == executed and response["remaining"] == remaining,
          f"incorrect executed/remaining quantities: {response}")
    return response


def rejected(connection, client_id, reason, instrument=None):
    response = acknowledgement(connection)
    check(response["status"] == 1 and response["reason"] == reason,
          f"expected reject reason {reason}: {response}")
    check(response["client_id"] == client_id, f"incorrect reject client id: {response}")
    check(response["order_id"] == 0 and response["executed"] == 0 and
          response["remaining"] == 0, f"rejection contains accepted-order state: {response}")
    if instrument is not None:
        check(response["instrument"] == instrument, f"incorrect reject instrument: {response}")


class Exchange:
    def __init__(self, executable):
        self.executable = executable
        self.process = None
        self.port = None

    def diagnostics(self):
        self.errors.flush()
        self.errors.seek(0)
        return self.errors.read().decode(errors="replace")

    def __enter__(self):
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            [self.executable, "--port", "0"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=self.errors)
        try:
            deadline = time.monotonic() + TIMEOUT
            buffered = b""
            ready = None
            while ready is None:
                remaining = deadline - time.monotonic()
                check(remaining > 0, "exchange did not become ready within five seconds")
                readable, _, _ = select.select([self.process.stdout], [], [], remaining)
                check(readable, "timed out waiting for exchange READY line")
                chunk = os.read(self.process.stdout.fileno(), 4096)
                check(chunk, "exchange exited before READY: " + self.diagnostics())
                buffered += chunk
                while b"\n" in buffered:
                    line, buffered = buffered.split(b"\n", 1)
                    text = line.decode(errors="replace")
                    if text.startswith("READY "):
                        ready = text
                        break
            fields = dict(re.findall(r"(\w+)=(\d+)", ready))
            check(all(name in fields for name in ("port", "core", "oeg_cpu", "matching_cpu")),
                  "incomplete READY placement evidence: " + ready)
            self.port = int(fields["port"])
            check(0 < self.port <= 65535, "exchange did not bind an ephemeral port")
            core = int(fields["core"])
            check(int(fields["oeg_cpu"]) == core == int(fields["matching_cpu"]),
                  "OEG and matching engine are not colocated: " + ready)
            if hasattr(os, "sched_getaffinity"):
                check(core in os.sched_getaffinity(0), "exchange chose a CPU outside allowed affinity")
            return self
        except BaseException:
            self.stop(require_success=False)
            raise

    def stop(self, require_success):
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                self.process.send_signal(signal.SIGTERM)
            try:
                code = self.process.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=TIMEOUT)
                raise AssertionError("exchange did not shut down after SIGTERM")
            if require_success:
                check(code == 0, f"exchange shutdown exit code {code}: {self.diagnostics()}")
        finally:
            self.process.stdout.close()
            self.errors.close()
            self.process = None

    def __exit__(self, error_type, error, traceback):
        self.stop(require_success=error_type is None)

    def connect(self):
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=TIMEOUT)
        connection.settimeout(TIMEOUT)
        connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return connection


def fragments_coalescing_and_sessions(executable):
    with Exchange(executable) as exchange, exchange.connect() as first:
        frame = order(1, side=2)
        # Force the framing header and body to arrive in separate application writes.
        first.sendall(frame[:1])
        check(not select.select([first], [], [], 0.02)[0],
              "exchange responded before receiving a complete framing header")
        first.sendall(frame[1:3])
        check(not select.select([first], [], [], 0.02)[0],
              "exchange responded before receiving a complete order")
        first.sendall(frame[3:])
        first_ack = accepted(first, 1, 0, 10)
        first.sendall(order(2, quantity=4) + order(3, quantity=6))
        second_ack = accepted(first, 2, 4, 0)
        third_ack = accepted(first, 3, 6, 0)
        check(first_ack["order_id"] < second_ack["order_id"] < third_ack["order_id"],
              "exchange order ids are not increasing")

        with exchange.connect() as second:
            # Client order ids belong to a connection; two sessions may use id 1.
            second.sendall(order(1, side=2, price=101, quantity=5))
            accepted(second, 1, 0, 5)
            first.sendall(order(4, price=101, quantity=5))
            accepted(first, 4, 5, 0)
            first.sendall(order(4, price=101, quantity=5))
            rejected(first, 4, 4, 1)


def matching_and_instrument_separation(executable):
    with Exchange(executable) as exchange, exchange.connect() as maker, exchange.connect() as taker:
        maker.sendall(order(1, side=2, price=101, quantity=3) +
                      order(2, side=2, price=101, quantity=7))
        accepted(maker, 1, 0, 3)
        accepted(maker, 2, 0, 7)
        taker.sendall(order(1, price=101, quantity=5))
        accepted(taker, 1, 5, 0)
        taker.sendall(order(2, price=101, quantity=7))
        accepted(taker, 2, 5, 2)

        # The unmatched bid on instrument 1 must not trade against instrument 2.
        maker.sendall(order(3, instrument=2, side=2, price=101, quantity=2))
        accepted(maker, 3, 0, 2, instrument=2)
        taker.sendall(order(3, instrument=2, price=101, quantity=2))
        accepted(taker, 3, 2, 0, instrument=2)
        maker.sendall(order(4, side=2, price=101, quantity=2))
        accepted(maker, 4, 2, 0)


def business_and_encoding_rejections(executable):
    with Exchange(executable) as exchange, exchange.connect() as connection:
        invalid_orders = (
            (order(1, side=7), 1, 2, 1),
            (order(2, price=0), 2, 2, 1),
            (order(3, price=-1), 3, 2, 1),
            (order(4, quantity=0), 4, 2, 1),
            (order(0), 0, 2, 1),
            (order(5, instrument=999), 5, 3, 999),
        )
        for frame, client_id, reason, instrument in invalid_orders:
            connection.sendall(frame)
            rejected(connection, client_id, reason, instrument)

        for frame in (order(10, block=28), order(11, template=99),
                      order(12, schema=99), order(13, version=1)):
            connection.sendall(frame)
            rejected(connection, 0, 1)
        # A framed schema error must not desynchronise later valid messages.
        connection.sendall(order(999, side=2, quantity=1))
        accepted(connection, 999, 0, 1)

        # Validation failures share the response ordering of matching-engine events.
        # An OEG-local immediate reject would overtake earlier accepted requests.
        connection.sendall(order(1000, side=2, price=200, quantity=1) +
                           order(1001, template=99) +
                           order(1002, price=1, quantity=1))
        accepted(connection, 1000, 0, 1)
        rejected(connection, 0, 1)
        accepted(connection, 1002, 0, 1)


def invalid_framing_closes_only_offending_session(executable):
    with Exchange(executable) as exchange:
        for header in (SOFH.pack(43, 0xFFFF), SOFH.pack(0, ENCODING),
                       SOFH.pack(13, ENCODING), SOFH.pack(257, ENCODING)):
            with exchange.connect() as connection:
                connection.sendall(header)
                try:
                    check(connection.recv(1) == b"", "invalid framing left the connection open")
                except ConnectionResetError:
                    pass
        with exchange.connect() as connection:
            connection.sendall(order(1))
            accepted(connection, 1, 0, 10)


def pipelining_and_half_close(executable):
    with Exchange(executable) as exchange, exchange.connect() as connection:
        count = 256
        connection.sendall(b"".join(order(client_id, side=1 if client_id % 2 else 2)
                                     for client_id in range(1, count + 1)))
        previous = 0
        for client_id in range(1, count + 1):
            response = accepted(connection, client_id, 0 if client_id % 2 else 10,
                                10 if client_id % 2 else 0)
            check(response["order_id"] > previous, "pipeline acknowledgements reordered")
            previous = response["order_id"]

        # TCP FIN ends requests, but complete requests already received still need responses.
        connection.sendall(b"".join(order(client_id, side=1 if client_id % 2 else 2)
                                     for client_id in range(count + 1, count + 65)))
        connection.shutdown(socket.SHUT_WR)
        for client_id in range(count + 1, count + 65):
            accepted(connection, client_id, 0 if client_id % 2 else 10,
                     10 if client_id % 2 else 0)
        check(connection.recv(1) == b"", "exchange did not finish a drained half-closed session")


def disconnect_with_outstanding_orders(executable):
    with Exchange(executable) as exchange:
        for _ in range(8):
            with exchange.connect() as connection:
                connection.sendall(b"".join(order(client_id, side=2, price=1000, quantity=1)
                                             for client_id in range(1, 65)))
                # Abruptly abandon replies while requests/events may still be outstanding.
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        with exchange.connect() as connection:
            connection.sendall(order(1, price=1, quantity=1))
            accepted(connection, 1, 0, 1)


def injector_smoke(exchange_executable, injector_executable):
    with Exchange(exchange_executable) as exchange:
        result = subprocess.run(
            [injector_executable, "--port", str(exchange.port), "--count", "1000", "--window", "64"],
            capture_output=True, text=True, timeout=15)
        check(result.returncode == 0, "injector failed: " + result.stdout + result.stderr)
        fields = dict(re.findall(r"(\w+)=(\d+)", result.stdout))
        check(fields.get("orders") == "1000" and fields.get("accepted") == "1000" and
              fields.get("rejected") == "0" and fields.get("executed_units") == "5000",
              "injector did not report the expected workload: " + result.stdout)


def command_line_validation(exchange_executable, injector_executable):
    for executable in (exchange_executable, injector_executable):
        result = subprocess.run([executable, "--help"], capture_output=True, text=True, timeout=TIMEOUT)
        check(result.returncode == 0 and "Usage:" in result.stdout,
              "--help did not succeed: " + result.stdout + result.stderr)
    bad_exchange_arguments = (("--unknown",), ("--port",), ("--port", "65536"),
                              ("--port", "abc"), ("--core", "256"), ("--core", "-1"))
    bad_injector_arguments = (("--unknown",), ("--count",), ("--count", "0"),
                              ("--window", "0"), ("--port", "0"), ("--quantity", "0"),
                              ("--price", "-1"), ("--host", "invalid-ip"))
    for executable, cases in ((exchange_executable, bad_exchange_arguments),
                              (injector_executable, bad_injector_arguments)):
        for arguments in cases:
            result = subprocess.run([executable, *arguments], capture_output=True,
                                    text=True, timeout=TIMEOUT)
            check(result.returncode != 0, f"accepted bad arguments: {executable} {arguments}")


def startup_failures(exchange_executable):
    with Exchange(exchange_executable) as exchange:
        arguments = (
            ("--port", "0", "--bind", "invalid-ip"),
            ("--port", "0", "--bind", "192.0.2.1"),
            ("--port", str(exchange.port), "--bind", "127.0.0.1"),
        )
        for case in arguments:
            result = subprocess.run([exchange_executable, *case], capture_output=True,
                                    text=True, timeout=TIMEOUT)
            check(result.returncode != 0, f"exchange accepted invalid startup configuration: {case}")
            check("READY " not in result.stdout,
                  f"exchange announced readiness despite startup failure: {case}")
        # Failed constructors must neither hang teardown nor disturb the active listener.
        with exchange.connect() as connection:
            connection.sendall(order(1))
            accepted(connection, 1, 0, 10)


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: integration_test.py EXCHANGE_EXECUTABLE INJECTOR_EXECUTABLE")
    exchange, injector = map(os.path.abspath, sys.argv[1:])
    cases = (
        ("fragmentation, coalescing and client sessions", lambda: fragments_coalescing_and_sessions(exchange)),
        ("matching and independent instruments", lambda: matching_and_instrument_separation(exchange)),
        ("business and encoding rejects", lambda: business_and_encoding_rejections(exchange)),
        ("invalid framing", lambda: invalid_framing_closes_only_offending_session(exchange)),
        ("pipelining and TCP half-close", lambda: pipelining_and_half_close(exchange)),
        ("disconnect with outstanding orders", lambda: disconnect_with_outstanding_orders(exchange)),
        ("external injector", lambda: injector_smoke(exchange, injector)),
        ("command-line validation", lambda: command_line_validation(exchange, injector)),
        ("startup failures and cleanup", lambda: startup_failures(exchange)),
    )
    for name, function in cases:
        function()
        print("PASS:", name, flush=True)


if __name__ == "__main__":
    main()
