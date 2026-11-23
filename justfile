export CXX := env("CXX", "g++-16")
export CMAKE_BUILD_PARALLEL_LEVEL := env("CMAKE_BUILD_PARALLEL_LEVEL", "2")

import 'redzone.just'

default:
    @just --list

configure mode="debug":
    cmake --preset "{{ mode }}"

build mode="debug": (configure mode)
    cmake --build --preset "{{ mode }}" --parallel

test mode="debug": (build mode)
    ctest --preset "{{ mode }}"

check: (test "debug") (test "release")

# Pass an optional matcher CPU ID; empty lets the exchange choose worker cores.
[positional-arguments]
exchange core="" mode="release" config="config/local.json": (build mode)
    #!/usr/bin/env bash
    set -euo pipefail
    args=(--config "$3")
    if [[ -n "$1" ]]; then
        args+=(--core "$1")
    fi
    exec "./build/$2/bin/ct_exchange" "${args[@]}"

[positional-arguments]
inject count="1000" window="1" price="10000" mode="release" logical_access="1" partition="1" firm="FIRM1": (build mode)
    #!/usr/bin/env bash
    set -euo pipefail
    exec "./build/$4/bin/ct_injector" --logical-access "$5" --partition "$6" --firm "$7" \
        --count "$1" --window "$2" --price "$3"

# Values for recipe argument `mode` (bash completion via `_completion_mode`).
[private]
_completion_mode:
    #!/usr/bin/env python3
    import json
    from pathlib import Path
    presets = json.loads(Path("CMakePresets.json").read_text())
    for preset in presets["configurePresets"]:
        if not preset.get("hidden"):
            print(preset["name"])
