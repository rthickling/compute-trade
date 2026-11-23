#!/usr/bin/env python3
"""Exercise Optiq startup, placement, and both command-line programs."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from acceptance import wire
from acceptance.driver import launch

TIMEOUT = 5


def startup_placement(executable, injector):
    with tempfile.TemporaryDirectory() as directory:
        placement = Path(directory) / "workers.json"
        with launch(executable, placement_file=placement) as exchange:
            assert not exchange.control_path.exists()
            data = json.loads(placement.read_text())
            workers = {worker["role"]: worker for worker in data["workers"]}
            assert set(workers) == {"gateway_matcher", "publisher", "bluezone"}
            assert len(data["workers"]) == 3
            tasks = Path(f'/proc/{data["process_id"]}/task')
            responsibilities = workers["bluezone"]["responsibilities"]
            assert sorted(responsibilities) == ["disconnect_replay", "order_transport", "statistics"]
            tids = {worker["thread_id"] for worker in workers.values()}
            assert len(tids) == 3
            startup_tid = data["engine_startup_thread_id"]
            assert startup_tid > 0
            assert startup_tid not in tids | {data["process_id"]}
            assert not (tasks / str(startup_tid)).exists()
            for worker in workers.values():
                assert (tasks / str(worker["thread_id"])).is_dir(), worker
                assert os.sched_getaffinity(worker["thread_id"]) == {worker["cpu"]}, worker
            subscriber = exchange.subscribe()
            client = exchange.connect()
            client.new_order(1, 1, wire.SELL, 100, 1)
            acknowledgement = client.reply(1)
            assert acknowledgement.template_id == wire.ACK, acknowledgement
            assert subscriber.read()["order_id"] == acknowledgement.fields["order_id"]

            injected = subprocess.run(
                [injector, "--logical-access", "2", "--port", exchange.ports["port"],
                 "--count", "2", "--window", "2", "--price", "100", "--quantity", "1"],
                capture_output=True, text=True, timeout=TIMEOUT,
            )
            assert injected.returncode == 0, injected
            assert "orders=2 accepted=2 rejected=0" in injected.stdout, injected
            removed, added = subscriber.consume(2)
            assert removed["action"] == "remove", removed
            assert removed["order_id"] == acknowledgement.fields["order_id"]
            assert added["action"] == "add", added

        control = Path(directory) / "test.sock"
        result = subprocess.run(
            [executable, "--config", str(Path(directory) / "unused.json"),
             "--redzone", "--acceptance-control", str(control)],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        assert result.returncode != 0 and "READY " not in result.stdout, result
        assert "--acceptance-control is incompatible with --redzone" in result.stderr, result
        assert not control.exists()


def required_options(exchange, injector):
    for executable, option in ((exchange, "--config"), (injector, "--logical-access")):
        result = subprocess.run(
            [executable], text=True, capture_output=True, timeout=TIMEOUT,
        )
        assert result.returncode != 0 and "READY " not in result.stdout, result
        assert option in result.stderr and "required" in result.stderr, result


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Usage: integration_test.py EXCHANGE_EXECUTABLE INJECTOR_EXECUTABLE")
    exchange, injector = map(os.path.abspath, sys.argv[1:])
    required_options(exchange, injector)
    startup_placement(exchange, injector)


if __name__ == "__main__":
    main()
