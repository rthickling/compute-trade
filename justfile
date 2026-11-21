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

# Pass an optional logical CPU ID; empty selects the first CPU allowed by affinity.
[positional-arguments]
exchange core="" mode="release": (build mode)
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ -n "$1" ]]; then
        exec "./build/$2/bin/ct_exchange" --core "$1"
    fi
    exec "./build/$2/bin/ct_exchange"

inject count="1000" window="1" price="10000" mode="release": (build mode)
    "./build/{{ mode }}/bin/ct_injector" --count "{{ count }}" --window "{{ window }}" --price "{{ price }}"

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
