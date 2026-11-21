"""Read Linux isolation state and qualify a Compute Trade worker placement."""

import argparse
import json
import os
import socket
import sys
from pathlib import Path

CPU = Path("/sys/devices/system/cpu")
WORKQUEUE = Path("/sys/devices/virtual/workqueue/cpumask")


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


def cpu_set(value):
    cpus = set()
    for segment in (value or "").split(","):
        if segment and segment != "(null)":
            limits = segment.split("-", 1)
            cpus.update(range(int(limits[0]), int(limits[-1]) + 1))
    return cpus


def hex_cpus(value):
    if value is None:
        return None
    bits = int(value.replace(",", ""), 16)
    return {cpu for cpu in range(bits.bit_length()) if bits & (1 << cpu)}


def irq_affinities():
    irqs = {}
    for entry in Path("/proc/irq").iterdir():
        if not entry.name.isdecimal():
            continue
        irq = int(entry.name)
        effective = read(entry / "effective_affinity_list")
        if effective is not None:
            irqs[irq] = sorted(cpu_set(effective))
    return dict(sorted(irqs.items()))


def job_cgroup(thread_id):
    for line in (read(Path(f"/proc/{thread_id}/cgroup")) or "").splitlines():
        if line.startswith("0::"):
            return line[3:]
    return ""


def observed_workers(args):
    if not (args.placement or args.control):
        return {}
    if args.control:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
            control.settimeout(4)
            control.connect(str(args.control))
            control.sendall(b'{"operation":"placement"}\n')
            with control.makefile("rb") as replies:
                data = json.loads(replies.readline(8192))
    else:
        data = json.loads(args.placement.read_text())
    if "result" in data:
        data = data["result"]
    for role, pid in (
        ("test_client", args.client_pid),
        ("subscriber", args.subscriber_pid),
    ):
        if pid is not None:
            stat = Path(f"/proc/{pid}/stat").read_text()
            # Fields following the parenthesised comm start with field 3.
            cpu = int(stat.rsplit(")", 1)[1].split()[36])
            data["workers"].append({"role": role, "thread_id": pid, "cpu": cpu})
    records = {}
    for worker in data["workers"]:
        role, tid = worker["role"], worker["thread_id"]
        if role in records:
            raise ValueError(f"duplicate placement role {role}")
        cgroup = job_cgroup(tid)
        cpuset = Path("/sys/fs/cgroup") / cgroup.lstrip("/") / "cpuset.cpus.effective"
        records[role] = {
            **worker,
            "affinity": sorted(os.sched_getaffinity(tid)),
            "scheduler": os.sched_getscheduler(tid),
            "priority": os.sched_getparam(tid).sched_priority,
            "cgroup": cgroup,
            "cpuset_effective": read(cpuset),
        }
    return records


def report(args):
    selected = {"gateway_matcher": args.matcher, "publisher": args.publisher}
    red = set(selected.values())
    online = cpu_set(read(CPU / "online"))
    siblings = {
        cpu: cpu_set(read(CPU / f"cpu{cpu}/topology/thread_siblings_list"))
        for cpu in online | red | {args.blue}
    }
    isolated = cpu_set(read(CPU / "isolated"))
    workqueue = hex_cpus(read(WORKQUEUE))
    irq = irq_affinities()
    residual = set(args.residual_irq)
    bad_irq = {
        number: cpus
        for number, cpus in irq.items()
        if number not in residual and red.intersection(cpus)
    }
    jobs = observed_workers(args)
    potential = []

    if not (red | {args.blue}) <= online:
        potential.append("some configured CPUs are offline")
    if (
        len(red) != 2
        or args.blue in red
        or any(
            second in siblings[first]
            for first, second in (
                (args.matcher, args.publisher),
                (args.matcher, args.blue),
                (args.publisher, args.blue),
            )
        )
    ):
        potential.append(
            "gateway, publisher and BlueZone require distinct physical cores"
        )
    if any((siblings[cpu] & online) != {cpu} for cpu in red):
        potential.append(
            "an SMT sibling of a reserved CPU is online or topology is unavailable"
        )
    if not red <= isolated:
        potential.append("reserved CPUs are missing from kernel boot isolation")
    if workqueue is None or workqueue & red:
        potential.append(
            "global workqueue CPU mask is unavailable or includes a reserved CPU"
        )
    if bad_irq:
        potential.append("one or more non-exempt IRQs can run on reserved CPUs")
    if not irq:
        potential.append("effective interrupt affinities were unavailable")
    if args.placement or args.control:
        for role, cpu in selected.items():
            worker = jobs.get(role)
            if worker is None:
                potential.append(f"missing worker {role}")
                continue
            if worker["cpu"] != cpu or worker["affinity"] != [cpu]:
                potential.append(f"{role} is not pinned to its reserved CPU")
            if worker["scheduler"] != os.SCHED_FIFO or worker["priority"] <= 0:
                potential.append(f"{role} lacks Simplx real-time scheduling")
        for role in ("statistics", "disconnect_replay"):
            worker = jobs.get(role)
            if (
                worker is None
                or worker["cpu"] != args.blue
                or worker["affinity"] != [args.blue]
            ):
                potential.append(f"{role} is not pinned to its BlueZone CPU")
        for role in ("test_client", "subscriber"):
            worker = jobs.get(role)
            if (
                worker is None
                or worker["cpu"] not in worker["affinity"]
                or red.intersection(worker["affinity"])
            ):
                potential.append(f"{role} housekeeper placement is missing or invalid")

    boot_filters = ("isolcpus=", "nohz_full=", "rcu_nocbs=", "irqaffinity=", "nosmt")
    cmdline = [
        part
        for part in (read(Path("/proc/cmdline")) or "").split()
        if part.startswith(boot_filters)
    ]
    power = {
        "boost": read(CPU / "cpufreq/boost"),
        "platform_profile": read(Path("/sys/firmware/acpi/platform_profile")),
        "cpus": {
            cpu: {
                name: read(CPU / f"cpu{cpu}/cpufreq/{name}")
                for name in (
                    "scaling_driver",
                    "scaling_governor",
                    "energy_performance_preference",
                )
            }
            for cpu in sorted(red | {args.blue})
        },
    }
    return {
        "result": "unqualified"
        if potential
        else ("qualified" if jobs else "preflight-ok"),
        "issues": potential,
        "kernel": os.uname().release,
        "settings": cmdline,
        "redzone": selected,
        "bluezone": args.blue,
        "online": sorted(online),
        "siblings": {cpu: sorted(values) for cpu, values in siblings.items()},
        "isolated": sorted(isolated),
        "nohz_full": sorted(cpu_set(read(CPU / "nohz_full"))),
        "workqueue_cpus": sorted(workqueue) if workqueue is not None else None,
        "housekeeping": sorted(online - red),
        "workers": list(jobs.values()),
        "irq_effective_affinity": irq,
        "reserved_irq_targets": bad_irq,
        "exempt_residual_irqs": sorted(residual),
        "power_settings": power,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matcher", type=int, required=True)
    parser.add_argument("--publisher", type=int, required=True)
    parser.add_argument("--blue", type=int, required=True)
    placement = parser.add_mutually_exclusive_group()
    placement.add_argument(
        "--placement", type=Path, help="read a saved placement response"
    )
    placement.add_argument(
        "--control",
        type=Path,
        help="read placement from the active Unix control socket",
    )
    parser.add_argument("--client-pid", type=int)
    parser.add_argument("--subscriber-pid", type=int)
    parser.add_argument("--residual-irq", type=int, action="append", default=[])
    parser.add_argument(
        "--strict", action="store_true", help="exit nonzero if isolation checks fail"
    )
    args = parser.parse_args()
    try:
        result = report(args)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"RedZone inspection failed: {error}\n")
    print(json.dumps(result, indent=2))
    return int(args.strict and bool(result["issues"]))


if __name__ == "__main__":
    sys.exit(main())
