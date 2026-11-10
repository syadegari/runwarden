"""Submit two GPU mocks, two 16-thread CPU mocks, or two native FE benchmarks.

Uses the installed rmgr CLI. Every invocation creates fresh private receipts and
a jobs.json index under build/demo-runs. Does not change service configuration.
"""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("gpu", "cpu", "fe"))
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--fe-repo", type=Path)
    parser.add_argument("--fe-lib", type=Path)
    parser.add_argument("--python", type=Path, help="FE py3.14 interpreter")
    parser.add_argument("--home", type=Path, help="Optional rmgr state directory")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600 or not 1 <= args.threads <= 256:
        parser.error("seconds must be 1..3600 and threads must be 1..256")
    root = Path(__file__).resolve().parents[1]
    cli = ["rmgr"] + (["--home", str(args.home.resolve())] if args.home else [])

    def query(*command):
        return json.loads(subprocess.check_output([*cli, *command, "--json"], text=True))

    resources = query("resources")
    health = query("health")
    if not health["accepting_jobs"]:
        parser.error("Daemon is not accepting jobs")
    if args.kind == "gpu" and resources["gpu_count"] != 1:
        parser.error("GPU reservations are disabled. Set gpu_count to 1 while the service is stopped, then start it.")
    cores = 1 if args.kind == "gpu" else args.threads
    if resources["physical_cores"] < cores:
        parser.error(f"Need {cores} configured cores")
    memory = "8G" if args.kind == "fe" else "1G"
    if resources["memory_bytes"] < (8 if args.kind == "fe" else 1) * 1024**3:
        parser.error(f"Need a configured memory budget of at least {memory}")
    commands = []
    if args.kind == "fe":
        if not all((args.fe_repo, args.fe_lib, args.python)):
            parser.error("fe requires --fe-repo, --fe-lib, and --python")
        native = args.fe_repo.resolve() / "native"
        library = args.fe_lib.resolve()
        # Preserve a venv/Conda interpreter path rather than resolving symlinks.
        interpreter = args.python.expanduser().absolute()
        for path in (native / "kkt_debug_1.h5", library, interpreter,
                     native / "benchmark_superlu_mt.py", native / "benchmark_superlu_mt_blocking.py"):
            if not path.is_file():
                parser.error(f"Missing {path}")
        common = [str(native / "kkt_debug_1.h5"), "t_n=0.02/iteration=2",
                  "--lib", str(library), "--threads", str(args.threads)]
        commands = [
            ("fe-compare", [str(interpreter), "-B", "-u", str(native / "benchmark_superlu_mt.py"), *common]),
            ("fe-blocking", [str(interpreter), "-B", "-u", str(native / "benchmark_superlu_mt_blocking.py"),
                             *common, "--repeats", "1", "--mode", "coupled", "--max-supernode", "100",
                             "--rowblk", "200", "--colblk", "100", "--allow-full-machine"]),
        ]
    else:
        executable = root / "build" / "mock_simulations" / f"mock_{args.kind}"
        if not executable.is_file():
            parser.error("Build first: bash examples/mock_simulations/build.sh")
        for mode in ("heat", "particles"):
            command = [str(executable), mode, str(args.seconds)]
            if args.kind == "cpu":
                command.append(str(args.threads))
            commands.append((f"{args.kind}-{mode}", command))
    output = root / "build" / "demo-runs"
    output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix=f"{args.kind}-", dir=output))
    jobs = []
    print(f"Run directory: {run}", flush=True)
    for name, command in commands:
        if name == "fe-blocking":
            command.extend(["--json", str(run / "blocking-results.json")])
        receipt = run / f"{name}.receipt.json"
        submit = [*cli, "submit", "--id-only", "--receipt", str(receipt), "--cwd", str(run),
                  "--cores", str(cores), "--memory", memory,
                  "--timeout", str(180 if args.kind == "fe" else args.seconds + 30)]
        if args.kind == "gpu":
            submit.append("--gpu")
        if args.kind == "fe":
            submit += ["--exclusive", "--env", "OPENBLAS_NUM_THREADS=1",
                       "--env", "MKL_NUM_THREADS=1", "--env", "OMP_NUM_THREADS=1",
                       "--env", "NUMEXPR_NUM_THREADS=1"]
        job_id = subprocess.check_output([*submit, "--", *command], text=True).strip()
        jobs.append({"name": name, "id": job_id, "receipt": str(receipt), "argv": command})
        (run / "jobs.json").write_text(json.dumps(jobs, indent=2) + "\n")
        print(f"{name}: {job_id}", flush=True)
    print(f"Saved IDs and receipt paths: {run / 'jobs.json'}", flush=True)


if __name__ == "__main__":
    main()
