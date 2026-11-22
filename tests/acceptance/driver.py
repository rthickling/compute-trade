"""Process, TCP session, subscriber, and test control clients."""

import json
import select
import socket
import struct
import subprocess
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Queue
from tempfile import TemporaryDirectory
from threading import Thread

from . import extensions, wire

HOST = "127.0.0.1"
TIMEOUT = 4.0
MAX_FRAME = 65535
MAX_JSON_LINE = 4 * 1024 * 1024
SESSION_FIXTURES = {
    (1, 1): ["FIRM1", "FIRM2"],
    (2, 1): ["FIRM1"],
    (3, 1): ["FIRM2"],
    (4, 1): ["FIRM3"],
    (1, 2): ["FIRM1", "FIRM2"],
}


def json_line(value):
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def read_json(stream):
    line = stream.readline(MAX_JSON_LINE + 1)
    if not line.endswith(b"\n") or len(line) > MAX_JSON_LINE:
        raise ValueError("missing or oversized JSON line")
    result = json.loads(line)
    if not isinstance(result, dict):
        raise TypeError(f"expected a JSON object, got {result!r}")
    return result


def read_exact(connection, count):
    received = bytearray()
    deadline = time.monotonic() + connection.gettimeout()
    configured_timeout = connection.gettimeout()
    try:
        while len(received) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out after {len(received)} of {count} bytes")
            connection.settimeout(remaining)
            chunk = connection.recv(count - len(received))
            if not chunk:
                raise EOFError(f"connection closed after {len(received)} of {count} bytes")
            received.extend(chunk)
    finally:
        connection.settimeout(configured_timeout)
    return bytes(received)


def read_frame(connection, prefix=b""):
    if len(prefix) > wire.HEADER.size:
        raise ValueError("frame prefix exceeds header length")
    header = prefix + read_exact(connection, wire.HEADER.size - len(prefix))
    length, *_ = wire.HEADER.unpack(header)
    if not wire.HEADER.size <= length <= MAX_FRAME:
        raise ValueError(f"invalid application frame length {length}")
    return header + read_exact(connection, length - wire.HEADER.size)


def check_fields(actual, expected):
    for name, value in expected.items():
        if name not in actual or actual[name] != value:
            raise AssertionError(f"expected {name}={value!r}, got {actual!r}")


class Session:
    def __init__(self, sock):
        self.socket = sock
        self.next_request_sequence = 1
        self.logon_ack = None
        self.replay = None
        self.replay_complete = None
        self.fills = []
        self.closed = False

    def close(self, reset=False):
        if self.closed:
            return
        try:
            if reset:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        finally:
            self.socket.close()
            self.closed = True

    def send(self, data):
        self.socket.sendall(data)

    def read(self, prefix=b""):
        return extensions.decode(read_frame(self.socket, prefix))

    def expect(self, template_id, **fields):
        message = self.read()
        if message.template_id != template_id:
            raise AssertionError(f"expected template {template_id}, got {message!r}")
        check_fields(message.fields, fields)
        if any(message.groups):
            raise AssertionError(f"unexpected populated optional groups in {message!r}")
        return message

    def expect_quiet(self, seconds=0.075):
        readable, _, _ = select.select([self.socket], [], [], seconds)
        if readable:
            detail = self.socket.recv(24, socket.MSG_PEEK)
            raise AssertionError(f"expected an idle connection, got bytes or EOF {detail!r}")

    def accept_logon(self):
        if self.logon_ack is None:
            self.logon_ack = self.expect(wire.LOGON_ACK)
            last_client_sequence = self.logon_ack.fields["last_client_sequence"]
            assert isinstance(last_client_sequence, int)
            self.next_request_sequence = max(
                self.next_request_sequence, last_client_sequence + 1
            )
        return self.logon_ack

    def finish_logon(self):
        self.accept_logon()
        if self.replay is None:
            replay = []
            while len(replay) < 4096:
                message = self.read()
                if message.template_id == extensions.REPLAY_COMPLETE:
                    self.replay = replay
                    self.replay_complete = message
                    return replay
                if message.template_id not in (
                    extensions.REPLAY_GAP, wire.ACK, wire.FILL, wire.KILL, wire.REJECT
                ):
                    raise AssertionError(f"unexpected message during replay: {message!r}")
                replay.append(message)
            raise AssertionError("replay did not finish within the acceptance safety bound")
        return self.replay

    def confirm(self, sequence):
        self.send(extensions.encode(extensions.CONFIRM, sequence=sequence))

    def _request(self, encode, *args, **kwargs):
        encoded = encode(*args, sequence=self.next_request_sequence, **kwargs)
        self.next_request_sequence += 1
        return encoded

    def encode_new_order(self, cid, instrument, side, price, quantity, *, firm="FIRM1"):
        return self._request(wire.new_order, cid, firm, instrument, side, price, quantity)

    def new_order(self, cid, instrument, side, price, quantity, *, firm="FIRM1"):
        self.send(self.encode_new_order(cid, instrument, side, price, quantity, firm=firm))

    def cancel(self, cid, order_id, *, instrument=1, side=wire.SELL, firm="FIRM1", original_id=None):
        self.send(self._request(wire.cancel, cid, firm, instrument, side, order_id,
                                original_client_order_id=original_id))

    def replace(self, cid, order_id, price, quantity, *, instrument=1, side=wire.SELL,
                firm="FIRM1", original_id=None, order_type=None):
        options = {"original_client_order_id": original_id}
        if order_type is not None:
            options["order_type"] = order_type
        self.send(self._request(wire.replace, cid, firm, instrument, side, order_id, price, quantity,
                                **options))

    def reply(self, cid):
        for _ in range(4096):
            message = self.read()
            if message.template_id == wire.FILL:
                self.fills.append(message)
                continue
            if message.template_id not in (wire.ACK, wire.KILL, wire.REJECT):
                raise AssertionError(f"expected a private reply, got {message!r}")
            check_fields(message.fields, {"client_order_id": cid})
            if any(message.groups):
                raise AssertionError(f"unexpected populated optional groups in {message!r}")
            return message
        raise AssertionError(f"reply for client order {cid} was not found")


class Subscriber:
    def __init__(self, sock):
        self.socket = sock
        self.stream = sock.makefile("rb")
        try:
            self.socket.sendall(json_line({"type": "subscribe"}))
            check_fields(read_json(self.stream), {"type": "ready", "sequence": 0})
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            self.stream.close()
        finally:
            self.socket.close()

    def read(self):
        return read_json(self.stream)

    def consume(self, count):
        return [self.read() for _ in range(count)]


class Exchange:
    def __init__(self, ports, control_path):
        self.ports = ports
        self.control_path = control_path
        self.connections = []
        self.injector: Path | None = None

    def raw_connection(self):
        sock = socket.create_connection((HOST, int(self.ports["port"])), timeout=TIMEOUT)
        self.connections.append(sock)
        return sock

    def connect(self, access=1, partition=1, last_processed=0, wait=True):
        session = Session(self.raw_connection())
        session.send(wire.logon(access, partition, last_processed))
        if wait:
            session.finish_logon()
        return session

    def subscribe(self):
        sock = socket.create_connection((HOST, int(self.ports["market_port"])), timeout=TIMEOUT)
        subscriber = Subscriber(sock)
        self.connections.append(subscriber)
        return subscriber

    def control(self, operation, **fields):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(TIMEOUT)
            connection.connect(str(self.control_path))
            with connection.makefile("rb") as stream:
                connection.sendall(json_line({"operation": operation, **fields}))
                response = read_json(stream)
        if response.get("ok") is not True or "result" not in response:
            raise AssertionError(f"control {operation} failed: {response!r}")
        return response["result"]

    def wait_for(self, event, **fields):
        result = self.control("wait", event=event, match=fields, timeout_ms=3000)
        check_fields(result, fields)
        return result

    def snapshot(self):
        return self.control("snapshot")

    def close(self):
        for connection in reversed(self.connections):
            connection.close()


@contextmanager
def launch(executable, limits=None, *, session_fixtures=None, env=None, placement_file=None):
    with TemporaryDirectory(prefix="ct-acceptance-") as raw_directory:
        directory = Path(raw_directory)
        config = directory / "exchange.json"
        control = directory / "control.sock"
        config.write_bytes(json_line({
            "instruments": [1, 2],
            "sessions": [
                {"logical_access_id": access, "partition_id": partition, "firms": firms}
                for (access, partition), firms in (
                    SESSION_FIXTURES if session_fixtures is None else session_fixtures
                ).items()
            ],
            "limits": limits or {},
        }))
        args = [
            str(executable), "--bind", HOST, "--port", "0", "--config", str(config),
            "--market-port", "0",
        ]
        if placement_file is None:
            args.extend(["--acceptance-control", str(control)])
        else:
            args.extend(["--placement-file", str(placement_file)])
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", env=env)
        readiness = Queue()
        lines = deque(maxlen=40)

        def capture():
            stdout = process.stdout
            assert stdout is not None
            with stdout:
                for line in stdout:
                    lines.append(line.rstrip())
                    if line.startswith("READY "):
                        readiness.put(line)
            readiness.put(None)

        output = Thread(target=capture, daemon=True)
        output.start()
        exchange = None
        failed = False
        try:
            try:
                line = readiness.get(timeout=TIMEOUT)
            except Empty as error:
                raise AssertionError("exchange startup timed out before READY") from error
            if line is None:
                raise AssertionError("exchange exited before READY")
            ports = dict(item.split("=", 1) for item in line.split()[1:])
            if any(not 0 < int(ports[name]) < 65536 for name in ("port", "market_port")):
                raise AssertionError(f"invalid exchange READY endpoint: {line!r}")
            exchange = Exchange(ports, control)
            yield exchange
        except BaseException as error:
            failed = True
            error.add_note("ct_exchange output (last 40 lines):\n" + "\n".join(lines))
            raise
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                result = process.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                result = process.wait()
                if not failed:
                    raise AssertionError("exchange did not stop after SIGTERM") from None
            finally:
                if exchange is not None:
                    exchange.close()
                output.join(timeout=TIMEOUT)
            if not failed and result != 0:
                raise AssertionError(f"exchange exited with status {result}:\n" + "\n".join(lines))
