# Validation and workloads

## Evidence from this development environment

- The suite runs under Python 3.12 and Conda `py3.14`; five host/socket checks
  remain skipped because the required execution capabilities are unavailable.
- Python scheduler/interface tests exercise core and memory admission, whole-job
  exclusivity, GPU serialization, FIFO fairness, dependency failure, idempotency,
  cancellation, lost launch replies, missing-unit recovery, logs, and CLI behavior.
- Token tests cover cross-agent cancellation denial, no disclosure through public
  queries, authenticated retries, receipt persistence before submission, old-schema
  migration without granting control, and the offline CLI manual.
- The wheel builds without downloading runtime dependencies and installs into
  a clean temporary virtualenv. The installed CLI can initialize configuration,
  display help, and report an unavailable daemon as a structured error.
- A source snapshot from `syadegari/fe_solver` main was retrieved using the GitHub
  connector because direct `git clone` could not resolve GitHub in the sandbox.
- Its `case_a_hex8.toml` ran in Conda `py3.14`, pinned to one core with one process
  and BLAS thread, stopping at t=0.1. It converged two increments; reported force
  balance was approximately 4.95e-15. This was a direct workload smoke test,
  not a run through the resource manager.
- The two-process FE smoke attempt was blocked by sandbox IPC permissions.
- The systemd bus and Unix listening sockets are blocked in this sandbox.
  Kernel cgroup enforcement, live daemon restart, and native parallel FE workloads
  therefore remain host-validation work. Pure scheduler tests do not prove them.
- NVIDIA driver access is unavailable here. GPU reservation logic is tested;
  real GPU execution is not verified.
- SuperLU_MT native compilation and PETSc execution were not tested. PETSc is
  on the upstream `feature/iterative-solvers` branch, not main.

## Run the host suite first

```bash
python3 -m unittest discover -s tests -v
RMGR_SYSTEMD_TESTS=1 python3 -m unittest tests.test_systemd_integration -v
```

The second command requires a functional user systemd manager and delegated
controllers. It uses private temporary state, no more than two physical cores,
and a 512 MiB aggregate budget. It checks real CPU affinity, memory OOM behavior,
walltime enforcement, descendant cleanup and scheduler/database recovery. It
does not install a service or change host settings. The manager should still be
treated as experimental until these checks pass on the target host.

## FE solver integration

Prepare a disposable checkout and use the existing `py3.14` executable. Keep
each run's deck and output directory separate: the solver resolves paths
relative to its TOML deck. Do not submit two jobs writing the same `run.h5`.
The manager does not automatically copy input data or make output directories
unique. Preserve both source and inputs until queued work has finished.

```bash
git clone https://github.com/syadegari/fe_solver.git /path/to/disposable/fe_solver
conda run -n py3.14 python -c 'import sys; print(sys.executable)'

python examples/fe_solver_job.py \
  --repo /path/to/disposable/fe_solver \
  --python /path/to/conda/envs/py3.14/bin/python \
  --deck /path/to/disposable/fe_solver/examples/case_a_hex8.toml \
  --cores 2 --processes 2 --stop-time 0.1 > /tmp/fe-job.json

rmgr submit --receipt ./fe-smoke-001.receipt.json --request-id fe-smoke-001 --spec /tmp/fe-job.json
```

This helper emits a specification; it does not submit work or modify the FE
checkout. It caps automated test requests at eight cores, in accordance with
that project's instructions. Start with the small default example before
increasing the mesh size or runtime. The default smoke limit is 120 seconds.

After building the upstream native bridge using its instructions, generate a
SuperLU_MT job with `--backend superlu_mt --threads 4 --cores 4 --processes 1`.
For a timing run, also use `--exclusive`. For the iterative-solvers branch,
inspect that revision's CLI and construct a JSON specification with its actual
PETSc/MPI arguments; this helper intentionally does not guess evolving options.
Local MPI children must stay in the job cgroup, and required executable/library
paths must be passed explicitly. Remote ranks are outside this tool's scope.

Sources: [FE solver README](https://github.com/syadegari/fe_solver),
[iterative solver development](https://github.com/syadegari/fe_solver/tree/feature/iterative-solvers).

## GPU smoke test

After the host driver works, configure `gpu_count: 1` and restart the daemon.
Submit `examples/gpu_smoke.py` twice with `--gpu` and distinct receipt paths
(default request IDs are random):

```bash
rmgr submit --receipt ./gpu-01.receipt.json --gpu --cores 1 --memory 1G --timeout 15 \
  -- python /absolute/path/to/examples/gpu_smoke.py
```

The script uses the CUDA driver through ctypes, fills a 16 MiB allocation,
verifies copied-back values, and exits after about two seconds. It requires no
CUDA toolkit or extra Python packages. The two jobs' start/finish timestamps
should not overlap. This is an access/serialization smoke test, not a performance
benchmark or proof that unmanaged programs cannot use the device.
The calls follow the [CUDA driver memory API](https://docs.nvidia.com/cuda/cuda-driver-api/cuda_driver_api/group__CUDA__MEM.html).
