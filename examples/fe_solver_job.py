"""Print a resource-manager specification for an existing FE solver checkout.

Each job needs its own deck/output directory. The checkout is used as cwd so
the requested source revision takes precedence over an editable Conda install.
"""

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True, help="py3.14 environment's Python executable")
    parser.add_argument("--deck", type=Path, required=True, help="A deck with an isolated output directory")
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--processes", type=int, default=2)
    parser.add_argument("--backend", choices=["scipy_splu", "superlu_mt"], default="scipy_splu")
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--memory-gib", type=int, default=4)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--stop-time", type=float, default=0.1)
    parser.add_argument("--exclusive", action="store_true")
    args = parser.parse_args()
    if not (1 <= args.cores <= 8 and 1 <= args.processes <= args.cores and 1 <= args.threads <= args.cores):
        parser.error("Keep automated FE checks within 1–8 cores; process/thread counts must fit the allocation")
    if args.memory_gib < 1 or args.timeout < 1 or args.stop_time <= 0:
        parser.error("Memory, timeout and stop-time must be positive")
    argv = [os.path.abspath(args.python), "-m", "fe_solver", "solve", str(args.deck.resolve()),
            "--num-processes", str(args.processes), "--solver-backend", args.backend,
            "--stop-time", str(args.stop_time)]
    if args.backend == "superlu_mt":
        argv.extend(["--num-threads", str(args.threads)])
    spec = {
        "argv": argv, "cwd": str(args.repo.resolve()), "cores": args.cores,
        "memory_bytes": args.memory_gib * 1024**3, "exclusive": args.exclusive,
        "timeout_seconds": args.timeout,
        "env": {"OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1", "HWLOC_COMPONENTS": "-gl"},
    }
    print(json.dumps(spec, indent=2))


if __name__ == "__main__":
    main()
