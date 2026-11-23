"""Read Linux isolation state and qualify a Compute Trade worker placement."""

import argparse
import json
import os
import socket
import sys
from pathlib import Path

CPU = Path("/sys/devices/system/cpu")
WORKQUEUE = Path("/sys/devices/virtual/workqueue/cpumask")
LEGACY_BLUE_RESPONSIBILITIES = ("statistics", "disconnect_replay")
BLUE_RESPONSIBILITIES = (*LEGACY_BLUE_RESPONSIBILITIES, "order_transport")


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


def workers_by_role(workers):
    rows = {}
    for worker in workers:
        role = worker["role"]
        if role in rows:
            raise ValueError(f"duplicate placement role {role}")
        rows[role] = worker

    aliases = [rows[role] for role in BLUE_RESPONSIBILITIES if role in rows]
    if "bluezone" in rows:
        responsibilities = rows["bluezone"].get("responsibilities")
        expected = (
            BLUE_RESPONSIBILITIES
            if isinstance(responsibilities, list) and "order_transport" in responsibilities
            else LEGACY_BLUE_RESPONSIBILITIES
        )
        if (
            aliases
            or not isinstance(responsibilities, list)
            or len(responsibilities) != len(expected)
            or any(role not in responsibilities for role in expected)
        ):
            raise ValueError("invalid BlueZone responsibilities")
    else:
        has_transport = "order_transport" in rows
        expected = BLUE_RESPONSIBILITIES if has_transport else LEGACY_BLUE_RESPONSIBILITIES
        same_thread = len(aliases) == len(expected) and all(
            worker["thread_id"] == aliases[0]["thread_id"] for worker in aliases
        )
        if has_transport and not same_thread:
            raise ValueError("BlueZone aliases must identify one thread")
        if same_thread:
            if any(worker["cpu"] != aliases[0]["cpu"] for worker in aliases):
                raise ValueError("BlueZone aliases disagree on the observed CPU")
            for role in expected:
                del rows[role]
            rows["bluezone"] = {
                **aliases[0], "role": "bluezone", "responsibilities": list(expected),
            }

    exchange_threads = [worker["thread_id"] for role, worker in rows.items()
                        if role not in ("test_client", "subscriber")]
    if len(set(exchange_threads)) != len(exchange_threads):
        raise ValueError("duplicate exchange thread IDs")
    return rows


def observed_workers(args):
    if not (args.placement or args.control):
        return {}, {"process_id": None}
    if args.control:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
            control.settimeout(4)
            control.connect(str(args.control))
            control.sendall(b'{"operation":"placement"}\n')
            with control.makefile("rb") as replies:
                data = json.loads(replies.readline(8192))
    else:
        data = json.loads(args.placement.read_text())
    if not isinstance(data, dict):
        raise ValueError("worker placement must be a JSON object")
    if isinstance(data.get("result"), dict):
        data = data["result"]
    process_id = data.get("process_id") if args.placement else None
    if process_id is not None:
        if type(process_id) is not int or process_id <= 0:
            raise ValueError("invalid exchange process ID in placement file")
        tasks = Path(f"/proc/{process_id}/task")
        for worker in data["workers"]:
            tid = worker["thread_id"]
            if type(tid) is not int or tid <= 0 or not (tasks / str(tid)).is_dir():
                raise ValueError(f"worker {tid} is not a live thread of exchange {process_id}")
    identity = {"process_id": process_id}
    if "engine_startup_thread_id" in data:
        tid = data["engine_startup_thread_id"]
        if type(tid) is not int or tid <= 0 or tid == process_id or any(
            worker["thread_id"] == tid for worker in data["workers"]
        ):
            raise ValueError("invalid engine startup thread ID in placement file")
        if process_id is None:
            raise ValueError("cannot verify engine startup thread without an exchange process ID")
        try:
            (tasks / str(tid)).stat()
        except FileNotFoundError:
            pass
        else:
            raise ValueError(f"engine startup thread {tid} is still present in exchange {process_id}")
        identity["engine_startup_thread_id"] = tid
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
    for role, worker in workers_by_role(data["workers"]).items():
        tid = worker["thread_id"]
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
    return records, identity


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
    jobs, identity = observed_workers(args)
    process_id = identity["process_id"]
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
    if args.blue in isolated:
        potential.append("BlueZone requires a housekeeping CPU")
    if workqueue is None or workqueue & red:
        potential.append(
            "global workqueue CPU mask is unavailable or includes a reserved CPU"
        )
    if bad_irq:
        potential.append("one or more non-exempt IRQs can run on reserved CPUs")
    if not irq:
        potential.append("effective interrupt affinities were unavailable")
    if args.placement or args.control:
        if process_id is not None and os.sched_getaffinity(process_id) != {args.blue}:
            potential.append("exchange supervisor is not pinned to housekeeping")
        for role, cpu in selected.items():
            worker = jobs.get(role)
            if worker is None:
                potential.append(f"missing worker {role}")
                continue
            if worker["cpu"] != cpu or worker["affinity"] != [cpu]:
                potential.append(f"{role} is not pinned to its reserved CPU")
            if worker["scheduler"] != os.SCHED_FIFO or worker["priority"] <= 0:
                potential.append(f"{role} lacks Simplx real-time scheduling")
        blue_roles = ("bluezone",) if "bluezone" in jobs else LEGACY_BLUE_RESPONSIBILITIES
        for role in blue_roles:
            worker = jobs.get(role)
            if (
                worker is None
                or worker["cpu"] != args.blue
                or worker["affinity"] != [args.blue]
            ):
                potential.append(f"{role} is not pinned to its BlueZone CPU")
            elif worker["scheduler"] != os.SCHED_OTHER:
                potential.append(f"{role} lacks ordinary Linux scheduling")
        # Only validate housekeepers that were explicitly sampled.
        for role, requested in (
            ("test_client", args.client_pid),
            ("subscriber", args.subscriber_pid),
        ):
            if requested is None:
                continue
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
        **identity,
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
        "--placement", type=Path, help="read startup placement or a saved placement response"
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
