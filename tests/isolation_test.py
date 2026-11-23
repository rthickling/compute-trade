#!/usr/bin/env python3
"""RedZone scheduling checks. Skips with status 77 unless isolated CPUs and RT priority are available."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from acceptance import wire
from acceptance.driver import Session

SKIP = 77


def skip(reason):
    print(f"SKIP: {reason}", file=sys.stderr)
    raise SystemExit(SKIP)


def read_ready(process, timeout):
    deadline = time.time() + timeout
    output = []
    while time.time() < deadline:
        if process.poll() is not None:
            output.append(process.stdout.read() or "")
            return None, "".join(output)
        line = process.stdout.readline()
        if not line:
            continue
        output.append(line)
        if line.startswith("READY "):
            return line.strip(), "".join(output)
    process.send_signal(signal.SIGINT)
    return None, "".join(output) + (process.stdout.read() or "")


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: isolation_test.py EXCHANGE_EXECUTABLE")
    isolated = Path("/sys/devices/system/cpu/isolated").read_text().strip()
    if not isolated:
        skip("no isolated CPUs")
    exchange = os.path.abspath(sys.argv[1])
    layout = json.loads(subprocess.check_output(
        [sys.executable, "tools/redzone_layout.py"], text=True))
    with tempfile.TemporaryDirectory() as directory:
        placement = Path(directory) / "workers.json"
        process = subprocess.Popen(
            [exchange, "--config", "config/local.json", "--bind", "127.0.0.1",
             "--port", "0", "--market-port", "0",
             "--core", str(layout["matcher"]),
             "--publisher-core", str(layout["publisher"]),
             "--blue-core", str(layout["blue"]),
             "--redzone", "--placement-file", str(placement)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            ready, output = read_ready(process, 10)
            if ready is None:
                if "Operation not permitted" in output or "Permission denied" in output:
                    skip("realtime scheduling is not permitted")
                raise AssertionError(output)
            fields = dict(item.split("=", 1) for item in ready.split()[1:])
            assert fields["scheduling"] == "redzone", ready
            workers = {row["role"]: row for row in json.loads(placement.read_text())["workers"]}
            assert set(workers) == {"gateway_matcher", "publisher", "bluezone"}
            for role, cpu in (("gateway_matcher", layout["matcher"]),
                              ("publisher", layout["publisher"])):
                worker = workers[role]
                assert worker["cpu"] == cpu, worker
                assert os.sched_getscheduler(worker["thread_id"]) == os.SCHED_FIFO, worker
                assert os.sched_getparam(worker["thread_id"]).sched_priority == 10, worker
                assert os.sched_getaffinity(worker["thread_id"]) == {cpu}, worker
            blue = workers["bluezone"]
            assert blue["cpu"] == layout["blue"], blue
            assert os.sched_getscheduler(blue["thread_id"]) == os.SCHED_OTHER, blue
            assert os.sched_getaffinity(blue["thread_id"]) == {layout["blue"]}, blue
            assert layout["blue"] not in set(layout["isolated_now"])

            session = Session(socket.create_connection(("127.0.0.1", int(fields["port"])), timeout=5))
            session.send(wire.logon(1, 1, 0))
            session.finish_logon()
            session.new_order(1, 1, wire.SELL, 100, 1)
            acknowledgement = session.reply(1)
            assert acknowledgement.template_id == wire.ACK, acknowledgement
            session.close()
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
