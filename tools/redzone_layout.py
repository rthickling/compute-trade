#!/usr/bin/env python3
"""Choose two RedZone CPUs and general-purpose housekeeping from this host.

RedZone uses two physical cores (matcher + publisher). Everything else stays
general-purpose; BlueZone runs on one housekeeping CPU under normal scheduling.

Selection rules (override with flags or REDZONE_* environment variables):
1. If the kernel already isolates exactly two CPUs, use those (sorted).
2. Otherwise take the two highest non-boot physical cores' primary threads.
3. BlueZone defaults to the highest remaining primary thread.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

CPU = Path("/sys/devices/system/cpu")


def cpu_set(value: str) -> set[int]:
    cpus: set[int] = set()
    for segment in (value or "").split(","):
        if not segment or segment == "(null)":
            continue
        limits = segment.split("-", 1)
        cpus.update(range(int(limits[0]), int(limits[-1]) + 1))
    return cpus


def read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def present_cpus() -> list[int]:
    return sorted(cpu_set(read(CPU / "present") or "0"))


def online_cpus() -> set[int]:
    text = read(CPU / "online")
    if text:
        return cpu_set(text)
    return set(present_cpus())


def physical_cores(online: set[int]) -> dict[tuple[int, int], list[int]]:
    cores: dict[tuple[int, int], list[int]] = {}
    for cpu in present_cpus():
        base = CPU / f"cpu{cpu}"
        if not (base / "topology").exists():
            continue
        if cpu not in online and (base / "online").exists():
            # Still record offline siblings so nosmt planning can prefer thread 0.
            pass
        sock = int(read(base / "topology/physical_package_id") or "0")
        core = int(read(base / "topology/core_id") or "0")
        cores.setdefault((sock, core), []).append(cpu)
    for key in cores:
        cores[key] = sorted(cores[key])
    return cores


def primary(cpus: list[int], online: set[int]) -> int | None:
    for cpu in cpus:
        if cpu in online:
            return cpu
    return cpus[0] if cpus else None


def isolated_cpus() -> set[int]:
    return cpu_set(read(CPU / "isolated") or "")


def boot_core(cores: dict[tuple[int, int], list[int]]) -> tuple[int, int] | None:
    for key, cpus in cores.items():
        if 0 in cpus:
            return key
    return None


def layout(matcher=None, publisher=None, blue=None) -> dict:
    online = online_cpus()
    cores = physical_cores(online)
    boot = boot_core(cores)
    primaries = []
    for key, cpus in sorted(cores.items()):
        if key == boot:
            continue
        cpu = primary(cpus, online)
        if cpu is not None:
            primaries.append(cpu)

    isolated = sorted(isolated_cpus() & online)
    if matcher is None and publisher is None and len(isolated) == 2:
        matcher, publisher = isolated[0], isolated[1]
    if matcher is None or publisher is None:
        if len(primaries) < 2:
            raise ValueError("need at least two non-boot physical cores for RedZone")
        # Highest-numbered physical cores: prefer leaving low cores for the OS.
        publisher = publisher if publisher is not None else primaries[-2]
        matcher = matcher if matcher is not None else primaries[-1]

    red = {matcher, publisher}
    if len(red) != 2:
        raise ValueError("matcher and publisher must be distinct CPUs")
    if not red <= online:
        missing = sorted(red - online)
        raise ValueError(f"RedZone CPUs not online: {missing}")

    # Primaries across every physical core (including the boot core).
    all_primaries = []
    for key, cpus in sorted(cores.items()):
        cpu = primary(cpus, online)
        if cpu is not None:
            all_primaries.append(cpu)
    house_primaries = [cpu for cpu in all_primaries if cpu not in red]
    if blue is None:
        nonboot_house = [cpu for cpu in house_primaries if boot is None or cpu not in cores.get(boot, [])]
        pool = nonboot_house or house_primaries
        if not pool:
            raise ValueError("no housekeeping primary CPU left for BlueZone")
        blue = pool[-1]
    if blue in red:
        raise ValueError("BlueZone CPU must not be a RedZone CPU")
    if blue not in online:
        raise ValueError(f"BlueZone CPU {blue} is not online")

    housekeeping = sorted(online - red)
    # Under nosmt, sibling threads go offline; boot cmdline irqaffinity should
    # name only the primary threads that remain for general-purpose work.
    boot_housekeeping = sorted(house_primaries)
    red_list = ",".join(map(str, sorted(red)))
    irq_list = ",".join(map(str, boot_housekeeping))
    siblings = {cpu: cores[key] for key, group in cores.items() for cpu in group}
    return {
        "matcher": matcher,
        "publisher": publisher,
        "blue": blue,
        "housekeeping": housekeeping,
        "housekeeping_list": ",".join(map(str, housekeeping)),
        "housekeeping_boot": boot_housekeeping,
        "housekeeping_boot_list": irq_list,
        "housekeeping_mask": format(sum(1 << cpu for cpu in housekeeping), "x"),
        "isolated_now": isolated,
        "online": sorted(online),
        "redzone_cmdline": (
            f"nosmt isolcpus=domain,managed_irq,{red_list} "
            f"nohz_full={red_list} rcu_nocbs={red_list} "
            f"irqaffinity={irq_list}"
        ),
        "siblings": {str(cpu): siblings.get(cpu, [cpu]) for cpu in sorted(online)},
    }


def env_int(name: str):
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matcher", type=int, default=env_int("REDZONE_MATCHER"))
    parser.add_argument("--publisher", type=int, default=env_int("REDZONE_PUBLISHER"))
    parser.add_argument("--blue", type=int, default=env_int("REDZONE_BLUE"))
    parser.add_argument(
        "--export",
        action="store_true",
        help="print shell assignments for eval",
    )
    parser.add_argument(
        "--cmdline",
        action="store_true",
        help="print only the suggested kernel command-line fragment",
    )
    args = parser.parse_args(argv)
    try:
        result = layout(args.matcher, args.publisher, args.blue)
    except ValueError as error:
        parser.exit(1, f"redzone layout: {error}\n")
    if args.cmdline:
        print(result["redzone_cmdline"])
        return 0
    if args.export:
        print(f"matcher_cpu={result['matcher']}")
        print(f"publisher_cpu={result['publisher']}")
        print(f"blue_cpu={result['blue']}")
        print(f"housekeeping={result['housekeeping_list']}")
        print(f"housekeeping_boot={result['housekeeping_boot_list']}")
        print(f"housekeeping_mask={result['housekeeping_mask']}")
        print(f"redzone_cmdline={result['redzone_cmdline']!r}")
        return 0
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
