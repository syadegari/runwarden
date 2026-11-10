#!/usr/bin/env bash
set -euo pipefail
repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
mkdir -p "$repo_dir/build/mock_simulations"
g++ -O3 -std=c++17 -Wall -Wextra -fopenmp \
    "$repo_dir/examples/mock_simulations/cpu.cpp" -o "$repo_dir/build/mock_simulations/mock_cpu"
nvcc -O3 -std=c++17 -arch=sm_75 \
    "$repo_dir/examples/mock_simulations/gpu.cu" -o "$repo_dir/build/mock_simulations/mock_gpu"
printf 'Built CPU and GPU simulations in %s/build/mock_simulations\n' "$repo_dir"
