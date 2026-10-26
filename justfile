export CXX := env_var_or_default("CXX", "g++-16")
export CMAKE_BUILD_PARALLEL_LEVEL := env_var_or_default("CMAKE_BUILD_PARALLEL_LEVEL", "2")

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

inject count="1000" window="1" mode="release": (build mode)
    "./build/{{ mode }}/bin/ct_injector" --count "{{ count }}" --window "{{ window }}"
