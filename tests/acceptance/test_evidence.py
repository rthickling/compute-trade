import copy
import unittest

from . import evidence


def workload_fixture():
    trace = {"near_best_ticks": 2, "scenarios": {}}
    runs = []
    for name in evidence.SCENARIOS:
        depth = 3 if name.startswith("deep") else 1
        quiet = name.endswith("quiet")
        phases = ("quiet", "quiet") if quiet else ("shock", "replenishment")
        book = [
            {
                "order_id": side,
                "instrument": 1,
                "side": side,
                "price": 100 + side,
                "quantity": depth,
            }
            for side in (1, 2)
        ]
        commands = [
            {
                "id": index,
                "operation": "new",
                "phase": phase,
                "planned_ns": index * 10,
                "instrument": 1,
                "side": 2 if index == 1 else 1,
                "price": 100 + index,
                "quantity": 1,
            }
            for index, phase in enumerate(phases, start=1)
        ]
        observed = [
            {
                "id": index,
                "best_bid": 101,
                "best_ask": 102,
                "available_buy_quantity": depth,
                "fills": [],
                "prices_crossed": [],
            }
            for index in (1, 2)
        ]
        scenario = {
            "initial_book": book,
            "instrument": 1,
            "seed": 42,
            "commands": commands,
            "observations": observed,
        }
        trace["scenarios"][name] = scenario
        runs.append(
            {
                "run_id": name,
                "scenario": name,
                "build_id": "build-a",
                "trace_id": evidence.trace_id(scenario),
                "market_data_enabled": True,
                "drain_until_ns": 60,
                "backlog_drained": True,
                "peak_outstanding": 2,
                "counts": {
                    "planned": 2,
                    "attempted": 2,
                    "submitted": 2,
                    "completed": 2,
                    "rejected": 1,
                    "incomplete": 0,
                },
                "requests": [
                    {
                        "id": 1,
                        "encoded_ns": 9,
                        "send_attempts_ns": [12, 16],
                        "submitted_ns": 17,
                        "reply_decoded_ns": 30,
                        "reply_correlated_ns": 31,
                        "outcome": "accepted",
                        "matcher_ns": 7,
                        "lateness_ns": 2,
                        "rtt_ns": 19,
                    },
                    {
                        "id": 2,
                        "encoded_ns": 19,
                        "send_attempts_ns": [24],
                        "submitted_ns": 26,
                        "reply_decoded_ns": 31,
                        "reply_correlated_ns": 32,
                        "outcome": "rejected",
                        "matcher_ns": 2,
                        "lateness_ns": 4,
                        "rtt_ns": 8,
                    },
                ],
            }
        )
    return trace, {"runs": runs}


def matcher_fixture():
    work = {
        "operation_counts": {"new": 5},
        "output_counts": {"trades": 2},
        "fill_count": 2,
        "ordered_fills": [[1, 10, 101, 1], [2, 10, 101, 1]],
        "maker_visits": 2,
        "price_visits": 1,
    }
    pairs = []
    for number, variants in enumerate(
        (("simplx_actor", "system"), ("system", "simplx_actor")), start=1
    ):
        runs = [
            {
                "run_id": f"{variant}-{number}",
                "variant": variant,
                "build_id": f"build-{variant}",
                "trace_id": "starting-book-and-command-digest",
                "timing_only": True,
                "work": copy.deepcopy(work),
                "samples_ns": [8, 10, 12],
                "sample_count": 3,
            }
            for variant in variants
        ]
        pairs.append(
            {
                "case_id": "recycled-cancel",
                "phase": "exploration",
                "working_set_bytes": 1024,
                "trace_id": "starting-book-and-command-digest",
                "runs": runs,
            }
        )
    return {
        "environment": {"cpu": "fixture", "opt_flags": "-O2"},
        "timing": {
            "start": "call",
            "stop": "result",
            "overhead_ns": 3,
            "fill_recording": "buffer",
            "event_preparation_included": False,
            "notice_preparation_included": True,
            "exclusions": [
                "tcp",
                "sbe_decode",
                "acknowledgements",
                "publication",
                "notice_replay",
                "statistics",
            ],
        },
        "pairs": pairs,
    }


class WorkloadEvidenceTest(unittest.TestCase):
    def test_reports_pressure_depth_and_by_phase_latency(self):
        trace, report = workload_fixture()
        result = evidence.validate_workloads(trace, copy.deepcopy(trace), report)
        self.assertEqual(result["traces"]["thin_shock"]["near_best_buy_quantity"], 1)
        self.assertEqual(result["traces"]["deep_shock"]["near_best_buy_quantity"], 3)
        self.assertEqual(
            result["runs"]["deep_shock"]["matcher_latency"],
            {
                "shock/new": {"sample_count": 1, "median_ns": 7},
                "replenishment/new": {"sample_count": 1, "median_ns": 2},
            },
        )

    def test_detects_regeneration_difference_and_unequal_sell_pressure(self):
        trace, report = workload_fixture()
        regenerated = copy.deepcopy(trace)
        regenerated["scenarios"]["thin_shock"]["commands"][0]["quantity"] += 1
        with self.assertRaisesRegex(ValueError, "regenerated evidence differs"):
            evidence.validate_workloads(trace, regenerated, report)
        with self.assertRaisesRegex(
            ValueError, "planned sell timing, size, price limit, or operation differs"
        ):
            evidence.validate_workloads(regenerated, regenerated, report)

    def test_reports_incomplete_submission_separately_from_unattempted(self):
        trace, report = workload_fixture()
        run = report["runs"][0]
        run["requests"] = [
            {
                "id": 1,
                "encoded_ns": 9,
                "send_attempts_ns": [12, 16],
                "submitted_ns": 17,
                "lateness_ns": 2,
            },
            {"id": 2, "send_attempts_ns": []},
        ]
        run["peak_outstanding"] = 1
        run["backlog_drained"] = False
        run["counts"] = {
            "planned": 2,
            "attempted": 1,
            "submitted": 1,
            "completed": 0,
            "rejected": 0,
            "incomplete": 1,
        }
        result = evidence.validate_workloads(trace, trace, report)["runs"][
            run["run_id"]
        ]
        self.assertEqual(result["counts"]["incomplete"], 1)
        run["backlog_drained"] = True
        with self.assertRaisesRegex(ValueError, "reported backlog drain differs"):
            evidence.validate_workloads(trace, trace, report)

    def test_detects_retries_resetting_lateness_or_rtt(self):
        trace, report = workload_fixture()
        row = report["runs"][0]["requests"][0]
        row["lateness_ns"] = 6
        with self.assertRaisesRegex(ValueError, "lateness must use the first"):
            evidence.validate_workloads(trace, trace, report)
        row["lateness_ns"] = 2
        row["rtt_ns"] = 15
        with self.assertRaisesRegex(ValueError, "RTT must use the first"):
            evidence.validate_workloads(trace, trace, report)

    def test_variants_must_process_same_ordered_work(self):
        trace, report = workload_fixture()
        run = report["runs"][0]
        run.update(
            comparison_id="same workload",
            ordered_fills=[[1, 2]],
            output_counts={"updates": 3},
        )
        other = copy.deepcopy(run)
        other.update(run_id="other variant", ordered_fills=[[2, 1]])
        report["runs"].append(other)
        with self.assertRaisesRegex(ValueError, "different inputs or ordered work"):
            evidence.validate_workloads(trace, trace, report)


class MatcherEvidenceTest(unittest.TestCase):
    def test_accepts_equal_and_reversed_performance(self):
        report = matcher_fixture()
        report["pairs"][1]["runs"][1]["samples_ns"] = [12, 14, 16]
        results = evidence.validate_matcher(report)["pairs"]
        self.assertEqual([pair["paired_difference_ns"] for pair in results], [0, -4])

    def test_checks_ordered_fills_trace_identity_and_timing_exclusions(self):
        report = matcher_fixture()
        run = report["pairs"][0]["runs"][1]
        run["work"]["ordered_fills"].reverse()
        with self.assertRaisesRegex(
            ValueError, "ordered fills or per-operation work differ"
        ):
            evidence.validate_matcher(report)
        run["work"]["ordered_fills"].reverse()
        run["trace_id"] = "different trace"
        with self.assertRaisesRegex(ValueError, "run trace ID differs"):
            evidence.validate_matcher(report)
        run["trace_id"] = report["pairs"][0]["trace_id"]
        report["timing"]["exclusions"].remove("notice_replay")
        with self.assertRaisesRegex(
            ValueError, "network or other excluded work included"
        ):
            evidence.validate_matcher(report)

    def test_checks_pair_rotation_and_sample_count(self):
        report = matcher_fixture()
        report["pairs"][1]["runs"].reverse()
        with self.assertRaisesRegex(ValueError, "did not rotate run order"):
            evidence.validate_matcher(report)
        report["pairs"][1]["runs"].reverse()
        report["pairs"][1]["runs"][1]["sample_count"] = 4
        with self.assertRaisesRegex(ValueError, "sample count mismatch"):
            evidence.validate_matcher(report)

    def test_cache_source_claim_requires_separate_evidence_and_denominator(self):
        report = matcher_fixture()
        report["cache_observations"] = [
            {"status": "unavailable", "reason": "No supported hardware event"}
        ]
        evidence.validate_matcher(report)
        report["cache_source_claims"] = True
        with self.assertRaisesRegex(ValueError, "claims require recorded evidence"):
            evidence.validate_matcher(report)
        capture = {
            "status": "recorded",
            "method": "IBS",
            "event": "ibs_op",
            "filter": "sampled maker quantity load",
            "instruction_map": "matching-build-symbols.txt",
            "raw_measurements": "ibs.data",
            "run_id": "sample-only",
            "timing_run_id": "simplx_actor-1",
            "build_id": "build-simplx_actor",
            "sample_count": 12,
            "lost_samples": 1,
            "denominator": 10,
            "cache_source_counts": {"L1": 8, "other": 2},
        }
        report["cache_observations"].append(capture)
        evidence.validate_matcher(report)
        capture["denominator"] = 11
        with self.assertRaisesRegex(
            ValueError, "cache-source counts or denominator disagree"
        ):
            evidence.validate_matcher(report)


if __name__ == "__main__":
    unittest.main()
