# RedZone recipes (docs/redzone.md). Imported by the root justfile.
# Two physical cores for matcher/publisher; everything else is general-purpose.
# Defaults: tools/redzone_layout.py (isolcpus if set, else two highest non-boot cores).
# Override CPUs positionally, e.g. `just redzone-layout 14 12 10`, or with
# REDZONE_MATCHER / REDZONE_PUBLISHER / REDZONE_BLUE in the environment.

redzone-layout matcher="" publisher="" blue="":
    #!/usr/bin/env bash
    set -euo pipefail
    args=()
    [[ -n "{{ matcher }}" ]] && args+=(--matcher "{{ matcher }}")
    [[ -n "{{ publisher }}" ]] && args+=(--publisher "{{ publisher }}")
    [[ -n "{{ blue }}" ]] && args+=(--blue "{{ blue }}")
    python3 tools/redzone_layout.py "${args[@]}"

redzone-grub:
    ./tools/install_redzone_grub.sh

redzone-preflight matcher="" publisher="" blue="":
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p build/redzone
    args=()
    [[ -n "{{ matcher }}" ]] && args+=(--matcher "{{ matcher }}")
    [[ -n "{{ publisher }}" ]] && args+=(--publisher "{{ publisher }}")
    [[ -n "{{ blue }}" ]] && args+=(--blue "{{ blue }}")
    python3 tools/redzone_layout.py "${args[@]}" | tee build/redzone/layout.json
    eval "$(python3 tools/redzone_layout.py --export "${args[@]}")"
    python3 tools/redzone_status.py --matcher "$matcher_cpu" --publisher "$publisher_cpu" \
        --blue "$blue_cpu" | tee build/redzone/preflight.json

redzone-exchange matcher="" publisher="" blue="" config="config/local.json" mode="release" port="9000" market_port="9001":
    #!/usr/bin/env bash
    set -euo pipefail
    just build "{{ mode }}"
    mkdir -p build/redzone
    args=()
    [[ -n "{{ matcher }}" ]] && args+=(--matcher "{{ matcher }}")
    [[ -n "{{ publisher }}" ]] && args+=(--publisher "{{ publisher }}")
    [[ -n "{{ blue }}" ]] && args+=(--blue "{{ blue }}")
    eval "$(python3 tools/redzone_layout.py --export "${args[@]}")"
    python3 tools/redzone_layout.py "${args[@]}" > build/redzone/layout.json
    control="$PWD/build/redzone/control.sock"
    rm -f "$control"
    echo "layout matcher=$matcher_cpu publisher=$publisher_cpu blue=$blue_cpu" >&2
    echo "control socket: $control" >&2
    # isolcpus removes reserved CPUs from the default affinity mask; taskset them back in
    # so validate_core / Simplx can place matcher and publisher threads.
    cpus="$matcher_cpu,$publisher_cpu,$blue_cpu"
    exec sudo systemd-run --wait --pipe --collect --uid="$(id -u)" --gid="$(id -g)" \
        -p LimitRTPRIO=10 --working-directory="$PWD" \
        taskset -c "$cpus" \
        "$PWD/build/{{ mode }}/bin/ct_exchange" --config "$PWD/{{ config }}" \
        --bind 127.0.0.1 --port "{{ port }}" --market-port "{{ market_port }}" \
        --core "$matcher_cpu" --publisher-core "$publisher_cpu" \
        --blue-core "$blue_cpu" --redzone --acceptance-control "$control"

redzone-subscribe housekeeping="" port="9001":
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p build/redzone
    if [[ -n "{{ housekeeping }}" ]]; then
        hk="{{ housekeeping }}"
    else
        eval "$(python3 tools/redzone_layout.py --export)"
        hk="$housekeeping"
    fi
    # Prefer post-nosmt primaries when the layout recorded them.
    if [[ -z "{{ housekeeping }}" ]]; then
        boot=$(python3 -c 'import json; print(",".join(map(str, json.load(open("build/redzone/layout.json")).get("housekeeping_boot", []))))' 2>/dev/null || true)
        [[ -n "$boot" ]] && hk="$boot"
    fi
    echo "pinning subscriber to $hk" >&2
    taskset -c "$hk" python3 tools/market_subscriber.py --port "{{ port }}" --quiet &
    echo $! | tee build/redzone/subscriber.pid

# residual: comma-separated IRQ numbers, or set REDZONE_RESIDUAL.
# Example: REDZONE_RESIDUAL=82,84 just redzone-placement
redzone-placement matcher="" publisher="" blue="" control="build/redzone/control.sock" residual="":
    #!/usr/bin/env bash
    set -euo pipefail
    args=()
    [[ -n "{{ matcher }}" ]] && args+=(--matcher "{{ matcher }}")
    [[ -n "{{ publisher }}" ]] && args+=(--publisher "{{ publisher }}")
    [[ -n "{{ blue }}" ]] && args+=(--blue "{{ blue }}")
    eval "$(python3 tools/redzone_layout.py --export "${args[@]}")"
    sub_args=()
    for role in subscriber client; do
        pid_file="build/redzone/${role}.pid"
        [[ -f "$pid_file" ]] || continue
        pid="$(tr -d '[:space:]' < "$pid_file")"
        [[ "$pid" =~ ^[0-9]+$ ]] || continue
        kill -0 "$pid" 2>/dev/null || continue
        sub_args+=(--"${role}-pid" "$pid")
    done
    residual_spec="{{ residual }}"
    if [[ -z "$residual_spec" ]]; then
        residual_spec="${REDZONE_RESIDUAL:-}"
    fi
    residual_args=()
    if [[ -n "$residual_spec" ]]; then
        IFS=',' read -r -a irq_list <<< "$residual_spec"
        for irq in "${irq_list[@]}"; do
            irq="${irq// /}"
            [[ -n "$irq" ]] && residual_args+=(--residual-irq "$irq")
        done
    fi
    python3 tools/redzone_status.py --strict --matcher "$matcher_cpu" \
        --publisher "$publisher_cpu" --blue "$blue_cpu" \
        --control "{{ control }}" "${sub_args[@]}" "${residual_args[@]}" \
        | tee build/redzone/placement.json

# Stop RedZone processes; reboot separately to clear nosmt/isolcpus.
redzone-stop:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -f build/redzone/subscriber.pid ]]; then
        pid=$(cat build/redzone/subscriber.pid)
        if kill -0 "$pid" 2>/dev/null; then
            echo "stopping subscriber pid $pid" >&2
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
        rm -f build/redzone/subscriber.pid
    fi
    # systemd-run units are named run-*.service; match our exchange binary.
    units=$(systemctl list-units --type=service --state=running --no-legend 'run-*.service' 2>/dev/null \
        | awk '{print $1}' || true)
    for unit in $units; do
        if systemctl show -p ExecStart "$unit" 2>/dev/null | grep -q 'ct_exchange'; then
            echo "stopping $unit" >&2
            sudo systemctl stop "$unit" || true
        fi
    done
    rm -f build/redzone/control.sock
    if [[ -f build/redzone/workqueue.before ]]; then
        echo "restoring workqueue cpumask" >&2
        sudo tee /sys/devices/virtual/workqueue/cpumask < build/redzone/workqueue.before >/dev/null
    fi
    shopt -s nullglob
    for saved in build/redzone/irq-*.before; do
        irq=${saved##*/irq-}
        irq=${irq%.before}
        echo "restoring IRQ $irq affinity" >&2
        sudo tee "/proc/irq/$irq/smp_affinity_list" < "$saved" >/dev/null || true
    done
    echo "Runtime RedZone processes cleared." >&2
    if grep -Eq 'isolcpus=|nosmt|nohz_full=' /proc/cmdline; then
        echo "This boot still has RedZone kernel flags. Reboot and pick the normal Ubuntu entry (or: just redzone-reboot)." >&2
    else
        echo "Kernel cmdline is already normal." >&2
    fi

# Stop RedZone processes and reboot into the default (normal) GRUB entry.
redzone-reboot: redzone-stop
    #!/usr/bin/env bash
    set -euo pipefail
    echo "Rebooting to the default (non-RedZone) GRUB entry…" >&2
    exec sudo reboot
