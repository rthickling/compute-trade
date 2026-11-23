"""Check Linux isolation evidence for a loaded RedZone exchange run."""

import argparse
import json
import os
import re
import sys
from pathlib import Path


LEGACY_BLUE_RESPONSIBILITIES = ("statistics", "disconnect_replay")
BLUE_RESPONSIBILITIES = (*LEGACY_BLUE_RESPONSIBILITIES, "order_transport")


def cpu_set(value):
    cpus = set()
    for segment in filter(None, value.strip().split(",")):
        first, last = (segment.split("-", 1) if "-" in segment else (segment, segment))
        start, end = int(first), int(last)
        if not 0 <= start <= end < 8192:
            raise ValueError(f"invalid Linux CPU list {value!r}")
        cpus.update(range(start, end + 1))
    return cpus


def capture(placement):
    """Read the actual Linux state for the process IDs returned by placement."""
    root = Path("/sys/devices/system/cpu")
    online = cpu_set((root / "online").read_text())
    isolated = cpu_set((root / "isolated").read_text())
    nohz = cpu_set((root / "nohz_full").read_text().replace("(null)", ""))
    workers = []
    for worker in placement["workers"]:
        tid = worker["thread_id"]
        workers.append({
            **worker,
            "affinity": sorted(os.sched_getaffinity(tid)),
            "scheduler": os.sched_getscheduler(tid),
            "priority": os.sched_getparam(tid).sched_priority,
        })
    return {
        **{key: placement[key] for key in ("process_id", "engine_startup_thread_id")
           if key in placement},
        "online": sorted(online),
        "isolated": sorted(isolated),
        "nohz_full": sorted(nohz),
        "siblings": {
            str(cpu): sorted(cpu_set((root / f"cpu{cpu}/topology/thread_siblings_list").read_text()))
            for cpu in online
        },
        "governors": {
            str(cpu): path.read_text().strip()
            for cpu in online
            if (path := root / f"cpu{cpu}/cpufreq/scaling_governor").exists()
        },
        "workers": workers,
    }


_TRACE_LINE = re.compile(r"\[(\d+)\]\s+(?:\S+\s+)?\d+\.\d+:\s+(sched_switch|irq_handler_entry):\s+(.*)")
_NEXT_PID = re.compile(r"(?:^|\s)next_pid=(\d+)(?:$|\s)")
_FORMATTED_NEXT_PID = re.compile(r"==>\s+.*:(\d+)\s+\[")
_IRQ = re.compile(r"(?:^|\s)irq=(\d+)(?:$|\s)")


def trace_events(lines):
    for line in lines:
        match = _TRACE_LINE.search(line)
        if match:
            cpu, kind, body = match.groups()
            if kind == "sched_switch":
                field = _NEXT_PID.search(body) or _FORMATTED_NEXT_PID.search(body)
            else:
                field = _IRQ.search(body)
            if field is None:
                raise ValueError(f"cannot parse {kind}: {line.rstrip()}")
            yield int(cpu), kind, int(field[1])
        elif "LOST EVENTS" in line.upper():
            raise ValueError("trace contains lost events")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_notice_burst(burst, sender_cpus):
    available = burst["notice_limit"] - burst["previously_unconfirmed"]
    maximum = min(available, burst["effective_live_limit"])
    require(burst["reader_delayed"] is True and maximum > 0,
            "allocation evidence needs a delayed notice consumer and an admissible burst")
    require(burst["cancelled_orders"] == maximum, "notice burst is smaller than the permitted limit")
    require(burst["peak_occupied"] == maximum + burst["previously_unconfirmed"],
            "notice peak does not include live reservations and prior notices")
    require(0 < burst["peak_outstanding"] <= burst["handoff_limit"], "invalid handoff peak")
    require(bool(burst["probe"]) and burst["lost_samples"] == 0,
            "missing fresh-allocation probe or incomplete allocation capture")
    counts = burst["fresh_event_pages_by_cpu"]
    require(sender_cpus == {int(key) for key in counts}
            and all(type(count) is int and count >= 0 for count in counts.values()),
            "missing or invalid fresh Event page counts on sending cores")


def validate(manifest, events):
    host = manifest["host"]
    require(manifest["completed_commands"] > 0, "trace must cover actual loaded exchange work")
    for name in ("run_id", "build_id", "kernel", "power_settings", "residual_interference"):
        require(bool(manifest[name]), f"missing {name}")
    online = set(host["online"])
    isolated = set(host["isolated"])
    # cpuset v2 partitions can isolate CPUs that are absent from /sys/.../isolated.
    isolated.update(manifest.get("verified_cpuset_isolated", []))
    by_role = {worker["role"]: worker for worker in host["workers"]}
    require(len(by_role) == len(host["workers"]), "duplicate placement roles")
    redzone = [by_role[role] for role in ("gateway_matcher", "publisher")]
    reserved = {worker["cpu"] for worker in redzone}
    require(len(reserved) == 2 and reserved <= online, "two distinct online RedZone CPUs required")
    require(reserved <= isolated, "RedZone CPUs are missing from verified isolated sets")
    for worker in redzone:
        cpu = worker["cpu"]
        siblings = set(host["siblings"][str(cpu)])
        require(siblings & online == {cpu}, f"CPU {cpu} has another online SMT thread")
        require(worker["affinity"] == [cpu], f"RedZone worker is not pinned to CPU {cpu}")
        require(worker["scheduler"] in (os.SCHED_FIFO, os.SCHED_RR) and worker["priority"] > 0,
                f"CPU {cpu} worker is missing real-time scheduling")

    blue_roles = LEGACY_BLUE_RESPONSIBILITIES
    if "bluezone" in by_role:
        responsibilities = by_role["bluezone"].get("responsibilities")
        expected = (
            BLUE_RESPONSIBILITIES
            if isinstance(responsibilities, list) and "order_transport" in responsibilities
            else LEGACY_BLUE_RESPONSIBILITIES
        )
        require(isinstance(responsibilities, list)
                and len(responsibilities) == len(expected)
                and all(role in responsibilities for role in expected)
                and all(role not in by_role for role in BLUE_RESPONSIBILITIES),
                "invalid BlueZone responsibilities")
        require(len({worker["thread_id"] for worker in (*redzone, by_role["bluezone"])}) == 3,
                "duplicate exchange thread IDs")
        blue_roles = ("bluezone",)
    elif "order_transport" in by_role:
        blue_roles = BLUE_RESPONSIBILITIES
        transport = by_role["order_transport"]
        require(all(role in by_role and by_role[role]["thread_id"] == transport["thread_id"]
                    and by_role[role]["cpu"] == transport["cpu"] for role in blue_roles),
                "BlueZone aliases must identify one thread and CPU")
        require(transport["thread_id"] not in {worker["thread_id"] for worker in redzone},
                "duplicate exchange thread IDs")

    for role in (*blue_roles, "test_client", "subscriber"):
        worker = by_role[role]
        require(bool(worker["affinity"]) and worker["cpu"] in worker["affinity"],
                f"{role} is missing observed or effective placement")
        require(not reserved.intersection(worker["affinity"]), f"{role} may run on RedZone CPUs")

    movable = set(manifest["movable_irqs"])
    documented_irqs = {entry["irq"] for entry in manifest["residual_irqs"] if entry["description"]}
    documented_threads = {
        entry["thread_id"] for entry in manifest["residual_threads"] if entry["description"]
    }
    for irq, affinity in manifest["irq_effective_affinity"].items():
        if int(irq) in movable:
            require(not reserved.intersection(affinity), f"movable IRQ {irq} can target RedZone")
    require(movable <= {int(irq) for irq in manifest["irq_effective_affinity"]},
            "missing effective affinity for movable IRQ")

    running = {worker["cpu"]: worker["thread_id"] for worker in redzone}
    observed_workers = set()
    irq_cpu_seen = set()
    for cpu, kind, identifier in events:
        if kind == "irq_handler_entry":
            irq_cpu_seen.add(cpu)
        if cpu not in reserved:
            continue
        if kind == "sched_switch":
            if identifier == running[cpu]:
                observed_workers.add(cpu)
            else:
                require(identifier == 0 or identifier in documented_threads,
                        f"unexpected thread {identifier} ran on {cpu}")
        elif kind == "irq_handler_entry":
            require(identifier not in movable, f"movable IRQ {identifier} ran on CPU {cpu}")
            require(identifier in documented_irqs, f"undocumented IRQ {identifier} ran on CPU {cpu}")
    require(observed_workers == reserved, "trace did not observe both RedZone workers scheduled")
    require(bool(irq_cpu_seen), "trace lacks IRQ evidence")
    validate_notice_burst(manifest["notice_burst"], reserved)
    return {"redzone_cpus": sorted(reserved), "irq_trace_cpus": sorted(irq_cpu_seen)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--capture", type=Path, metavar="PLACEMENT_JSON")
    modes.add_argument("--verify", nargs=2, type=Path, metavar=("MANIFEST_JSON", "TRACE_REPORT"))
    args = parser.parse_args(argv)
    try:
        if args.capture:
            result = capture(json.loads(args.capture.read_text()))
        else:
            manifest, report = args.verify
            with report.open() as trace:
                result = validate(json.loads(manifest.read_text()), trace_events(trace))
        print(json.dumps(result, indent=2))
    except (KeyError, OSError, ValueError) as error:
        print(f"Invalid RedZone evidence: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
