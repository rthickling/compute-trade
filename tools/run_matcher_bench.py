"""Run paired matcher-only allocator trials and write reproducible evidence."""

import argparse
import gzip
import hashlib
import json
import math
import os
import platform
import subprocess
import tempfile
import time
from pathlib import Path
from statistics import median


def read_host(path):
    source = Path(path)
    return source.read_text().strip() if source.exists() else None


def digest(value):
    encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_configuration(executable):
    build_directory = executable.resolve().parent.parent
    result = {}
    if commands := read_host(build_directory / "compile_commands.json"):
        result["compile_command"] = next(
            (
                entry["command"]
                for entry in json.loads(commands)
                if entry["file"].endswith("/src/matcher_bench.cpp")
            ),
            None,
        )
    if cache := read_host(build_directory / "CMakeCache.txt"):
        for entry in cache.splitlines():
            if entry.startswith("CMAKE_BUILD_TYPE:STRING="):
                result["build_type"] = entry.split("=", 1)[1]
            if entry.startswith("SIMPLX_SOURCE_DIR:PATH="):
                simplx_tree = entry.split("=", 1)[1]
                version = subprocess.run(
                    ["git", "-C", simplx_tree, "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                result["simplx_revision"] = (
                    version.stdout.strip() if version.returncode == 0 else None
                )
    return result


MODES = {
    "match": {
        "start": "steady_clock before first matcher preparation in a sample",
        "stop": "steady_clock after the last restoration command commits",
        "fill_recording": "copy each ordered fill to reused sample storage in both variants",
        "title": "Matched sweeps and restoration",
        "unit": "sweep",
    },
    "prepare": {
        "start": "steady_clock before first static-book matcher preparation in a sample",
        "stop": "steady_clock after the final preparation and ordered fill copy",
        "fill_recording": "copy each ordered prospective fill to reused sample storage in both variants",
        "title": "Preparation on a static book",
        "unit": "preparation",
    },
    "recycle": {
        "start": "steady_clock before reading and removing the first FIFO node in a sample",
        "stop": "steady_clock after appending the last replacement FIFO node",
        "fill_recording": "no trades; each retired order ID is recorded and checked outside timing",
        "title": "FIFO node retirement and replacement",
        "unit": "node recycled",
    },
}


def case_trace(args, orders, samples):
    sweeps = args.sweeps_per_sample * samples
    initial_book = {"ask_price": 100, "quantity_per_order": 1, "order_count": orders}
    if args.mode == "recycle":
        return {
            "initial_book": initial_book,
            "command_pattern": {
                "pop_front_then_push_back": args.fills,
                "sweeps": sweeps,
            },
        }
    if args.mode == "prepare":
        initial_book["instruments"] = orders // args.fills
        initial_book["orders_per_instrument"] = args.fills
        return {
            "initial_book": initial_book,
            "command_pattern": {
                "prepare_buy": {"price": 100, "quantity": args.fills},
                "instrument_rotation": "ascending IDs starting at 1, then repeat",
                "commit": False,
                "sweeps": sweeps,
            },
        }
    return {
        "initial_book": initial_book,
        "command_pattern": {
            "take": {
                "instrument": 1,
                "side": "buy",
                "price": 100,
                "quantity": args.fills,
            },
            "restore": {
                "instrument": 1,
                "side": "sell",
                "price": 100,
                "quantity": 1,
                "repetitions": args.fills,
            },
            "sweeps": sweeps,
        },
    }


def evidence(args):
    digest_binary = hashlib.sha256()
    with args.executable.open("rb") as binary:
        for block in iter(lambda: binary.read(1024 * 1024), b""):
            digest_binary.update(block)
    build_id = digest_binary.hexdigest()
    origin = time.time_ns()
    report = {
        "environment": {
            "platform": platform.platform(),
            "cpu": args.cpu,
            "cpu_allowed": sorted(os.sched_getaffinity(0)),
            "cpu_model": next(
                (
                    line.split(":", 1)[1].strip()
                    for line in Path("/proc/cpuinfo").read_text().splitlines()
                    if line.startswith("model name")
                ),
                None,
            ),
            "cpu_governor": read_host(
                f"/sys/devices/system/cpu/cpu{args.cpu}/cpufreq/scaling_governor"
            ),
            "cpu_siblings": read_host(
                f"/sys/devices/system/cpu/cpu{args.cpu}/topology/thread_siblings_list"
            ),
            "cpu_isolated": read_host("/sys/devices/system/cpu/isolated"),
            "scheduling": "Simplx worker pinned to one CPU using ordinary scheduling",
            "libc": platform.libc_ver(),
            **build_configuration(args.executable),
        },
        "timing": {
            **MODES[args.mode],
            "mode": args.mode,
            "event_preparation_included": False,
            "notice_preparation_included": False,
            "exclusions": [
                "tcp",
                "sbe_decode",
                "acknowledgements",
                "publication",
                "notice_replay",
                "statistics",
                "initial_book",
                "warmup",
                "artifact_serialization",
            ],
            "sweeps_per_sample": args.sweeps_per_sample,
            "operations_per_sweep": args.fills if args.mode == "recycle" else 1,
        },
        "pairs": [],
        "cache_observations": [
            {
                "status": "unavailable",
                "reason": "This run collects timings only. No hardware cache-source samples were collected.",
            }
        ],
    }

    overheads = []
    for orders in args.orders:
        samples = args.samples
        if samples is None:
            samples = max(16, math.ceil(orders / (args.fills * args.sweeps_per_sample)))
        trace = case_trace(args, orders, samples)
        trace_id = digest(trace)
        name = f"{args.mode}-fifo-{orders}-group-{args.fills}"
        for index in range(args.pairs):
            order = ("actor", "system") if index % 2 == 0 else ("system", "actor")
            pair = {
                "case_id": name,
                "phase": args.phase,
                "trace_id": trace_id,
                "trace_description": trace,
                "runs": [],
            }
            for variant in order:
                command = [
                    str(args.executable.resolve()),
                    variant,
                    str(args.cpu),
                    str(orders),
                    str(args.fills),
                    str(args.sweeps_per_sample),
                    str(samples),
                    args.mode,
                ]
                response = subprocess.run(
                    command, check=True, capture_output=True, text=True
                )
                run = json.loads(response.stdout)
                if run["cpu"] != args.cpu:
                    raise ValueError(
                        f"{variant} did not run on selected CPU {args.cpu}"
                    )
                if run["mode"] != args.mode:
                    raise ValueError(f"{variant} did not run the selected mode")
                report["environment"]["compiler"] = run["compiler"]
                footprint = orders * run["record_size"]
                if pair.get("working_set_bytes", footprint) != footprint:
                    raise ValueError("paired book footprints differ")
                pair["working_set_bytes"] = footprint
                pair["working_set_definition"] = (
                    "live order record bytes, excluding links and indexes"
                )
                overheads.append(run["timer_overhead_ns"])
                pair["runs"].append(
                    {
                        "run_id": f"{origin}-{args.phase}-{name}-{index}-{variant}",
                        "variant": variant,
                        "build_id": build_id,
                        "trace_id": trace_id,
                        "timing_only": True,
                        "work": run["work"],
                        "samples_ns": run["samples_ns"],
                        "sample_count": len(run["samples_ns"]),
                        "timer_overhead_ns": run["timer_overhead_ns"],
                        "argv": command,
                    }
                )
            first, second = pair["runs"]
            if first["work"] != second["work"]:
                raise ValueError(
                    f"{name} did not produce equivalent ordered matcher work"
                )
            report["pairs"].append(pair)
    report["timing"]["overhead_ns"] = int(median(overheads))
    return report


def figure(path, report):
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FixedLocator, FuncFormatter

    timing = report["timing"]
    batch = timing["sweeps_per_sample"] * timing.get("operations_per_sweep", 1)
    unit = timing.get("unit", "sweep")
    markers = {"actor": "o", "system": "x"}
    colours = {"actor": "#99462e", "system": "#274e72"}
    figure, axes = plt.subplots(1, 2, figsize=(11, 4.2), layout="constrained")
    seen = set()
    positive_samples = True
    footprints = set()
    for pair in report["pairs"]:
        footprint = pair["working_set_bytes"]
        footprints.add(footprint)
        medians = {}
        for run in pair["runs"]:
            variant = run["variant"]
            latencies = [sample / batch for sample in run["samples_ns"]]
            positive_samples = positive_samples and min(latencies) > 0
            medians[variant] = median(latencies)
            axes[0].scatter(
                [footprint] * len(latencies),
                latencies,
                color=colours[variant],
                alpha=0.22,
                s=10,
                marker=markers[variant],
                label=variant if variant not in seen else None,
            )
            seen.add(variant)
            axes[0].scatter(
                [footprint],
                [medians[variant]],
                color=colours[variant],
                s=45,
                marker=markers[variant],
            )
        axes[1].scatter(
            [footprint],
            [medians["actor"] - medians["system"]],
            color="#2d6647",
            marker="o",
        )
    axes[0].set_title(timing.get("title", "Matched sweeps and restoration"))
    axes[0].set_ylabel(f"Nanoseconds per {unit}")
    if positive_samples:
        axes[0].set_yscale("log")
    axes[0].legend()
    axes[1].set_title("Paired median difference")
    axes[1].set_ylabel(f"Actor minus system (ns per {unit})")
    axes[1].axhline(0, color="#5a6268", linewidth=0.8)

    def format_bytes(value, _):
        if value >= 1024**2:
            return f"{value / 1024**2:g} MiB"
        if value >= 1024:
            return f"{value / 1024:g} KiB"
        return f"{value:g} B"

    for axis in axes:
        axis.set_xscale("log", base=2)
        axis.xaxis.set_major_locator(FixedLocator(sorted(footprints)))
        axis.xaxis.set_major_formatter(FuncFormatter(format_bytes))
        axis.set_xlabel("Live order record bytes")
        axis.grid(alpha=0.18)
    figure.savefig(path)
    plt.close(figure)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, help="built ct_matcher_bench")
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--plot-report", type=Path, help="chart a saved JSON or gzip JSON report"
    )
    parser.add_argument(
        "--figure", type=Path, help="optional chart, requires matplotlib"
    )
    parser.add_argument("--cpu", type=int, default=min(os.sched_getaffinity(0)))
    parser.add_argument("--orders", type=int, nargs="+", default=[256, 4096, 32768])
    parser.add_argument("--fills", type=int, default=1)
    parser.add_argument("--sweeps-per-sample", type=int, default=128)
    parser.add_argument("--samples", type=int)
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--mode", choices=MODES, default="match")
    parser.add_argument(
        "--phase", choices=["exploration", "confirmation"], default="exploration"
    )
    args = parser.parse_args()
    if args.plot_report:
        if not args.figure or args.executable or args.report:
            parser.error("--plot-report requires --figure and reads an existing report")
        open_report = gzip.open if args.plot_report.suffix == ".gz" else open
        try:
            with open_report(args.plot_report, "rt", encoding="utf-8") as report:
                figure(args.figure, json.load(report))
        except (ImportError, OSError, ValueError) as error:
            parser.exit(1, f"Could not create the chart: {error}\n")
        return
    if not args.executable or not args.report:
        parser.error("benchmark trials require --executable and --report")
    if min(*args.orders, args.fills, args.sweeps_per_sample, args.pairs) <= 0:
        parser.error("counts must be positive")
    if len(set(args.orders)) != len(args.orders):
        parser.error("each working-set size must be listed once")
    if args.mode == "prepare" and any(orders % args.fills for orders in args.orders):
        parser.error("--mode prepare needs a whole number of FIFO groups")
    if args.samples is not None and args.samples <= 0:
        parser.error("sample count must be positive")

    temporary = None
    try:
        report = evidence(args)
        if args.figure:
            figure(args.figure, report)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=args.report.parent,
            prefix=f".{args.report.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, separators=(",", ":"))
            stream.write("\n")
        temporary.replace(args.report)
    except subprocess.CalledProcessError as error:
        parser.exit(
            1, f"Benchmark exited with status {error.returncode}: {error.stderr}"
        )
    except (ImportError, OSError, ValueError) as error:
        parser.exit(1, f"Could not complete the benchmark: {error}\n")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
