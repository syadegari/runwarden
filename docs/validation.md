# Validation and workloads

## Host validation on 2026-11-10

The source checkout at `72df7dc` passed all 47 tests with no skips using:

```bash
RMGR_SYSTEMD_TESTS=1 python3 -m unittest discover -s tests -v
```

The run took 4.376 seconds with host systemd and Unix-socket access explicitly
approved outside the restricted sandbox. Live checks verified assigned CPU
affinity, overlapping jobs on disjoint CPU allocations, memory OOM and walltime
enforcement, cancellation of descendants, and completion recovery after reopening
SQLite and replacing the scheduler. The socket transport test also passed.
This recovery test does not restart the production daemon or reboot the host.

The existing service had no running or queued jobs before the tests. Tests used
private temporary state and did not change host configuration or the installed
package. The service's pipx installation still contained the older GPU reporting
code (`gpu_device_access`); these results validate the source checkout, not an
upgrade of that installation. Real GPU and managed FE workloads remain unverified
by this run.

## Native FE interoperability on 2026-11-10

Two jobs were subsequently submitted through the installed daemon using the
helper in [demo jobs](demo-jobs.md). They read the native benchmark scripts and
`kkt_debug_1.h5` from FE revision `a613901`, using an existing Release/PTHREAD
bridge with bundled BLAS from another local checkout. The Python backend wrappers
in the two checkouts were byte-identical. The interpreter was Conda `py3.14`.
The machine was 99.9% idle in a one-second CPU sample before submission and the
managed queue was empty.

Both jobs reserved CPU IDs 0–15, 8 GiB and exclusive execution, with 180-second
timeouts. Nested BLAS pools were set to one thread; SuperLU_MT was explicitly
given 16 threads. Both exited 0 with empty stderr. Persisted start/finish times
confirmed that the second started after the first finished.

- `benchmark_superlu_mt.py`: approximately 29.91 seconds total. On the
  17,193-unknown KKT system (1,068,651 stored entries), SciPy factorization took
  25.806 seconds and SuperLU_MT factorization took 3.447 seconds. Residual infinity
  norms were `4.129319e-15` and `4.079419e-15`, respectively. However, the script
  printed `match=False`: maximum solution difference was `1.278989e-11` and
  relative 2-norm difference was `9.536770e-10`. Its comparison uses elementwise
  `allclose(rtol=1e-9, atol=1e-11)` and does not fail its exit status on mismatch.
  This confirms execution interoperability, not a passing numerical comparison;
  the small mismatch alone does not establish the documented upstream race.
- `benchmark_superlu_mt_blocking.py`: approximately 7.19 seconds total, with
  one default and one `max_supernode=100` factorization. Kernel times were
  3.280 and 3.100 seconds. This timing-only benchmark does not check solutions;
  one sample per configuration does not establish a stable speedup.

The run directory under ignored `build/demo-runs/` retains job IDs, receipts,
copied stdout/stderr and the blocking JSON report. Runwarden also retains its
original logs and job records. This checks native sparse-solver integration;
it does not exercise a full nonlinear FE simulation or an upstream race sweep.

## Mock workload checks on 2026-11-10

Both CUDA mocks in `examples/mock_simulations/` compiled with the installed
CUDA 12.8 toolkit and passed two-second runs on an RTX 4070 Ti SUPER. They ran
through the current source's systemd backend/scheduler using isolated persisted
state with GPU reservations enabled, after the main queue was empty. Both
finished with finite-value and conservation checks passing. The two OpenMP
variants also passed two-second runs with two threads each. Job timestamps
confirmed serial execution. Full stdout/stderr and job records remain under
ignored `build/demo-runs/mock-validation-*/`.

These checks did not change the production daemon's `gpu_count: 0` configuration.
They establish actual GPU kernel execution through the backend, not a production
GPU configuration upgrade. The 16-thread native jobs above used the production
daemon; the CPU mocks were only smoke-tested at two threads.

## Earlier sandbox evidence

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
conda run -n py3.14 python -c 'import sys; print(sys.executable)'

rmgr submit --receipt ./fe-smoke-001.receipt.json \
  --cores 2 --memory 4G --timeout 120 \
  --cwd /path/to/disposable/fe_solver \
  --env OMP_NUM_THREADS=1 --env OPENBLAS_NUM_THREADS=1 \
  --env MKL_NUM_THREADS=1 --env NUMEXPR_NUM_THREADS=1 --env HWLOC_COMPONENTS=-gl \
  -- /path/to/conda/envs/py3.14/bin/python -m fe_solver solve \
  /path/to/disposable/fe_solver/examples/case_a_hex8.toml \
  --num-processes 2 --solver-backend scipy_splu --stop-time 0.1
```

This launches the solver directly through the manager; no submission script is
needed. Start with the small default example before increasing the mesh size or
runtime. `examples/fe_solver_job.py` is an optional JSON-specification generator,
useful for structured automation, and caps automated requests at eight cores.

After building/configuring the upstream native bridge using its instructions,
reserve `--cores 4` before `--`, and pass `--solver-backend superlu_mt
--num-threads 4 --num-processes 1` to the solver after `--`. For a timing run,
also give the manager `--exclusive`. For the iterative-solvers branch, inspect
that revision's CLI and submit its actual PETSc/MPI command after `--`.
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
