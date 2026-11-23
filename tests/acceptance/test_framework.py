"""Check the test framework's own parsers and evidence checks."""

import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from . import extensions, hardware
from .driver import check_fields, read_json

PLACEMENT_TOOL = Path(__file__).resolve().parents[2] / "tools/redzone_status.py"


class SessionControlTest(unittest.TestCase):
    def test_replay_gap_golden_bytes(self):
        packet = bytes.fromhex("12000800c900010001000500000008000000")
        message = extensions.decode(packet)
        self.assertEqual(message.template_id, extensions.REPLAY_GAP)
        self.assertEqual(message.fields, {"first_sequence": 5, "last_sequence": 8})
        self.assertEqual(
            extensions.encode(extensions.REPLAY_GAP, **message.fields), packet
        )

    def test_bad_gaps_and_lengths(self):
        for fields in [
            {"first_sequence": 0, "last_sequence": 1},
            {"first_sequence": 4, "last_sequence": 3},
            {"first_sequence": 4, "last_sequence": 0xFFFFFFFF},
        ]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                extensions.encode(extensions.REPLAY_GAP, **fields)
        frame = extensions.encode(extensions.REPLAY_COMPLETE, sequence=0)
        with self.assertRaisesRegex(ValueError, "length"):
            extensions.decode(frame + b"\0")

    def test_control_json_requires_objects_and_terminator(self):
        payload = io.BytesIO(b'{"status":"ok"}\n')
        check_fields(read_json(payload), {"status": "ok"})
        with self.subTest(value=b"[]\n"), self.assertRaises(TypeError):
            read_json(io.BytesIO(b"[]\n"))
        with self.subTest(value=b'{"ok":true}'), self.assertRaises(ValueError):
            read_json(io.BytesIO(b'{"ok":true}'))
        with self.assertRaisesRegex(AssertionError, "absent"):
            check_fields({}, {"absent": None})


class HardwareEvidenceTest(unittest.TestCase):
    def setUp(self):
        workers = [
            {
                "role": "gateway_matcher",
                "cpu": 1,
                "thread_id": 501,
                "affinity": [1],
                "scheduler": 1,
                "priority": 6,
            },
            {
                "role": "publisher",
                "cpu": 2,
                "thread_id": 502,
                "affinity": [2],
                "scheduler": 1,
                "priority": 6,
            },
        ] + [
            {
                "role": role,
                "cpu": 0,
                "thread_id": 600 + idx,
                "affinity": [0],
                "scheduler": 0,
                "priority": 0,
            }
            for idx, role in enumerate(
                ("statistics", "disconnect_replay", "test_client", "subscriber")
            )
        ]
        self.manifest = {
            "host": {
                "workers": workers,
                "isolated": [1, 2],
                "online": [0, 1, 2],
                "siblings": {"1": [1, 5], "2": [2, 6]},
            },
            "completed_commands": 200,
            "run_id": "run",
            "build_id": "build",
            "kernel": "test",
            "power_settings": "fixture",
            "residual_interference": "fixture",
            "movable_irqs": [10],
            "residual_irqs": [{"irq": 2, "description": "fixture"}],
            "residual_threads": [],
            "irq_effective_affinity": {"10": [0]},
            "notice_burst": {
                "notice_limit": 10,
                "previously_unconfirmed": 2,
                "effective_live_limit": 9,
                "reader_delayed": True,
                "cancelled_orders": 8,
                "peak_occupied": 10,
                "peak_outstanding": 2,
                "handoff_limit": 2,
                "probe": "fixture",
                "lost_samples": 0,
                "fresh_event_pages_by_cpu": {"1": 0, "2": 1},
            },
        }
        self.trace = [
            "a-0 [001] 22.001: sched_switch: prev_pid=0 ==> next_pid=501 next_prio=40",
            "b-0 [002] d..2 22.002: sched_switch: swapper:0 [120] R ==> publisher:502 [40]",
            "a-0 [001] 22.003: irq_handler_entry: irq=2 name=fixture",
            "z-0 [000] 22.004: irq_handler_entry: irq=10 name=fixture",
        ]

    def test_isolation_checks_schedule_and_irqs(self):
        result = hardware.validate(self.manifest, hardware.trace_events(self.trace))
        self.assertEqual(result["redzone_cpus"], [1, 2])

    def test_single_bluezone_worker_with_current_or_legacy_responsibilities(self):
        workers = self.manifest["host"]["workers"]
        statistics, replay = workers[2:4]
        legacy = [statistics["role"], replay["role"]]
        for responsibilities in (legacy, [*legacy, "order_transport"]):
            with self.subTest(responsibilities=responsibilities):
                fixture = deepcopy(self.manifest)
                blue = fixture["host"]["workers"]
                blue[2:4] = [{
                    **statistics, "role": "bluezone", "responsibilities": responsibilities,
                }]
                result = hardware.validate(fixture, hardware.trace_events(self.trace))
                self.assertEqual(result["redzone_cpus"], [1, 2])

                blue.append({**statistics, "role": "order_transport"})
                with self.assertRaisesRegex(ValueError, "invalid BlueZone responsibilities"):
                    hardware.validate(fixture, hardware.trace_events(self.trace))

    def test_order_transport_aliases_identify_one_placement(self):
        workers = self.manifest["host"]["workers"]
        statistics, replay = workers[2:4]
        replay["thread_id"] = statistics["thread_id"]
        transport = {**statistics, "role": "order_transport"}
        workers.append(transport)
        result = hardware.validate(self.manifest, hardware.trace_events(self.trace))
        self.assertEqual(result["redzone_cpus"], [1, 2])

        transport["thread_id"] = 999
        with self.assertRaisesRegex(ValueError, "BlueZone aliases"):
            hardware.validate(self.manifest, hardware.trace_events(self.trace))

    def test_rejects_active_sibling_or_other_work(self):
        smt = deepcopy(self.manifest)
        smt["host"]["online"].append(5)
        with self.assertRaisesRegex(ValueError, "SMT"):
            hardware.validate(smt, hardware.trace_events(self.trace))

        unexpected = [
            *self.trace,
            "x-0 [001] 22.005: sched_switch: prev_pid=501 ==> next_pid=777",
        ]
        with self.assertRaisesRegex(ValueError, "unexpected thread"):
            hardware.validate(self.manifest, hardware.trace_events(unexpected))

        wrong_irq = [
            *self.trace,
            "z-0 [002] 22.006: irq_handler_entry: irq=10 name=fixture",
        ]
        with self.assertRaisesRegex(ValueError, "movable IRQ"):
            hardware.validate(self.manifest, hardware.trace_events(wrong_irq))

    def test_trace_and_cpu_parser_fail_closed(self):
        self.assertEqual(hardware.cpu_set("0-2,4,6-7\n"), {0, 1, 2, 4, 6, 7})
        with self.assertRaises(ValueError):
            hardware.cpu_set("5-3")
        with self.assertRaisesRegex(ValueError, "lost"):
            list(hardware.trace_events(["CPU:0 [LOST EVENTS]"]))
        with self.assertRaisesRegex(ValueError, "both"):
            hardware.validate(self.manifest, hardware.trace_events(self.trace[1:]))

    def test_notice_burst_requires_maximum_and_all_sending_cores(self):
        short = deepcopy(self.manifest)
        short["notice_burst"]["cancelled_orders"] -= 1
        with self.assertRaisesRegex(ValueError, "smaller"):
            hardware.validate(short, hardware.trace_events(self.trace))
        short["notice_burst"]["cancelled_orders"] += 1
        del short["notice_burst"]["fresh_event_pages_by_cpu"]["2"]
        with self.assertRaisesRegex(ValueError, "sending cores"):
            hardware.validate(short, hardware.trace_events(self.trace))


class PlacementStatusTest(unittest.TestCase):
    def test_placement_normalizes_legacy_and_current_aliases(self):
        cpu = min(os.sched_getaffinity(0))
        with TemporaryDirectory() as directory:
            placement = Path(directory) / "workers.json"
            command = [
                sys.executable, str(PLACEMENT_TOOL), "--matcher", str(cpu), "--publisher",
                str(cpu), "--blue", str(cpu), "--placement", str(placement),
            ]
            legacy = ["statistics", "disconnect_replay"]
            for responsibilities in (legacy, [*legacy, "order_transport"]):
                with self.subTest(responsibilities=responsibilities):
                    placement.write_text(json.dumps({
                        "process_id": os.getpid(),
                        "workers": [{"role": role, "cpu": cpu, "thread_id": os.getpid()}
                                    for role in responsibilities],
                    }))
                    result = subprocess.run(command, capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 0, result)
                    (blue,) = json.loads(result.stdout)["workers"]
                    check_fields(blue, {"role": "bluezone", "responsibilities": responsibilities})

    def test_startup_thread_must_be_absent_from_exchange_tasks(self):
        process_id = os.getpid()
        cpu = str(min(os.sched_getaffinity(0)))
        with TemporaryDirectory() as directory:
            placement = Path(directory) / "workers.json"
            command = [
                sys.executable, str(PLACEMENT_TOOL), "--matcher", cpu, "--publisher", cpu,
                "--blue", cpu, "--placement", str(placement),
            ]
            stop = threading.Event()
            startup = threading.Thread(target=stop.wait)
            startup.start()
            try:
                tid = startup.native_id
                placement.write_text(json.dumps({
                    "process_id": process_id,
                    "engine_startup_thread_id": tid,
                    "workers": [{"role": "gateway_matcher", "cpu": int(cpu),
                                 "thread_id": process_id}],
                }))
                result = subprocess.run(command, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 1, result)
                self.assertIn(f"engine startup thread {tid} is still present", result.stderr)
            finally:
                stop.set()
                startup.join()

            task = Path(f"/proc/{process_id}/task/{tid}")
            deadline = time.monotonic() + 1
            while task.exists() and time.monotonic() < deadline:
                time.sleep(0.001)
            self.assertFalse(task.exists())
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result)
            self.assertEqual(json.loads(result.stdout)["engine_startup_thread_id"], tid)
