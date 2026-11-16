# Validation and workloads

This is a dated evidence record, not a claim that every current change has been
tested on every host. Commit references use the documentation-only history
rewrite; the corresponding tested runtime code is unchanged.

| Date / candidate | Result | Scope |
| --- | --- | --- |
| 2026-11-14: original host-setup patch on base `4e08c9c` | 97 tests passed, no skips | Disposable Ubuntu VM; real systemd/cgroups, installation, operator cancellation, logout and reboot checks. |
| 2026-11-14: audited revision, now `69e412f` | 91 passed, 8 skipped (99 total) | Local suite; deadline regression included. Revised code was not rerun in the VM. |
| 2026-11-11: 0.3 output separation | 60 passed, no skips | Host systemd, output handling, and persistence; additional focused and installed-wheel checks below. |
| 2026-11-10: source at `4a9d3de` | 47 passed, no skips | Host enforcement and recovery; FE and GPU checks recorded separately below. |

- [Installation and recovery checks](#installation-and-recovery-checks-on-2026-11-14)
- [Output separation audit](#output-separation-and-consistency-audit-on-2026-11-11)
- [Host validation](#host-validation-on-2026-11-10)
- [Native FE interoperability](#native-fe-interoperability-on-2026-11-10)
- [Mock workloads](#mock-workload-checks-on-2026-11-10)
- [Earlier sandbox evidence](#earlier-sandbox-evidence)
- [Run the host suite](#run-the-host-suite-first)
- [FE solver integration](#fe-solver-integration)
- [GPU smoke test](#gpu-smoke-test)

## Installation and recovery checks on 2026-11-14

A disposable Ubuntu 24.04 guest used QEMU TCG software emulation and an official
cloud image with its signed checksum verified. The initial guest had no Runwarden
installation or delegation repairs. The candidate was the supplied host-setup
patch on base `4e08c9c`, before the subsequent audit corrections.

The first installation reproduced two blockers: pipx's executable directory was
absent from PATH, and the user manager did not delegate `cpuset`. After an explicit
PATH correction and guest-only delegation change followed by reboot:

- `doctor`, service installation, and `health --wait 30` passed.
- A submitted job verified its working directory, assigned CPU affinity, and
  separate stdout/stderr contents.
- The full suite with `RMGR_SYSTEMD_TESTS=1` passed all 97 tests without skips,
  including real cgroup memory OOM, walltime, and descendant-cancellation checks.
- A virtualenv installation passed on the same repaired guest, with the service
  confirmed to use that environment's interpreter.
- Ordinary-user `admin-cancel` was denied; guest-root cancellation succeeded and
  recorded the root peer UID in the job's audit history.
- With lingering enabled, the service survived logout and started after another
  reboot. Completed-job history and output remained retrievable.

The audit then corrected the readiness deadline behavior and setup instructions.
That revision, now `69e412f`, passed 91 local tests with eight host-dependent skips
(99 total), including deterministic deadline regressions. It was not rerun in
the VM. The VM evidence therefore does not establish a live test of those final
corrections. Shell examples and offline command discovery were also checked.

The VM was shut down and its reservation released. Host production software and
configuration were not changed. This was one guest configuration, not a distro
matrix, GPU test, or performance benchmark. The virtualenv check reused the
repaired guest; reboot checks covered completed jobs, not running-job survival.

## Output separation and consistency audit on 2026-11-11

The 0.3 source changes were checked with the complete 60-test suite using host
systemd/Unix-socket access: all tests passed with no skips (6.414 seconds).
This includes live custom output paths, launch diagnostics kept separate from
program stderr, collisions that fail only the affected job, and legacy queued
jobs retaining central output paths. Filesystem tests also cover default paths,
permissions, persistence, schema/receipt compatibility, missing or replaced
outputs, and retries after cwd/resource changes.

The installed production daemon was ready with zero running/queued jobs before
testing. Tests used temporary state with at most two cores and 512 MiB; they did
not upgrade that installation or modify its configuration. See the
[audit report](code-documentation-audit.md) for scope, corrections and limits.
After the API readiness fix, all 39 interface/output/control tests passed.
An offline-built 0.3 wheel also passed a disposable installed-daemon check:
one job continued through an actual daemon-process restart, then both program
streams were read through the CLI from its working-directory output files.
The later offline instructions command brought the suite to 61 tests; its
sandbox run passed 53 tests with eight host-only checks skipped.

## Host validation on 2026-11-10

The source checkout corresponding to `4a9d3de` passed all 47 tests with no skips using:

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
upgrade of that installation. GPU and FE workloads were checked separately below.

## Native FE interoperability on 2026-11-10

Two jobs were subsequently submitted through the installed daemon using the
helper in [demo jobs](demo-jobs.md). They read the native benchmark scripts and
`kkt_debug_1.h5` from FE revision `a613901`, using an existing Release/PTHREAD
bridge with bundled BLAS from another local checkout. The Python backend wrappers
in the two checkouts were byte-identical. The interpreter was Conda `py3.14`.
The managed queue was empty before submission.

Both jobs reserved CPU and memory capacity with exclusive execution and bounded
timeouts. Nested BLAS pools were set to one thread; SuperLU_MT's worker count
matched the CPU reservation. Both exited 0 with empty stderr. Persisted
start/finish times confirmed that the second started after the first finished.

- `benchmark_superlu_mt.py`: on the
  17,193-unknown KKT system (1,068,651 stored entries), SciPy and SuperLU_MT
  residual infinity norms were `4.129319e-15` and `4.079419e-15`, respectively. However, the script
  printed `match=False`: maximum solution difference was `1.278989e-11` and
  relative 2-norm difference was `9.536770e-10`. Its comparison uses elementwise
  `allclose(rtol=1e-9, atol=1e-11)` and does not fail its exit status on mismatch.
  This confirms execution interoperability, not a passing numerical comparison;
  the small mismatch alone does not establish the documented upstream race.
- `benchmark_superlu_mt_blocking.py`: completed
  one default and one `max_supernode=100` factorization.
  This timing-only benchmark does not check solutions;
  one sample per configuration does not establish a stable speedup.

The run directory under ignored `build/demo-runs/` retains job IDs, receipts,
copied stdout/stderr and the blocking JSON report. Runwarden also retains its
original logs and job records. This checks native sparse-solver integration;
it does not exercise a full nonlinear FE simulation or an upstream race sweep.

## Mock workload checks on 2026-11-10

Both CUDA mocks in `examples/mock_simulations/` compiled with a compatible
CUDA toolkit and passed two-second runs on a CUDA-capable GPU. They ran
through the current source's systemd backend/scheduler using isolated persisted
state with GPU reservations enabled, after the main queue was empty. Both
finished with finite-value and conservation checks passing. The two OpenMP
variants also passed two-second runs with two threads each. Job timestamps
confirmed serial execution. Full stdout/stderr and job records remain under
ignored `build/demo-runs/mock-validation-*/`.

These checks did not change the production daemon's `gpu_count: 0` configuration.
They establish actual GPU kernel execution through the backend, not a production
GPU configuration upgrade. The native FE jobs above used the production
daemon; the CPU mocks were only smoke-tested at two threads.

## Earlier sandbox evidence

- The earlier suite ran under Python 3.12 and Conda `py3.14`; five host/socket
  checks were skipped inside the restricted sandbox. Later approved host
  validation is recorded above.
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
- The sandbox blocked systemd and Unix sockets. Subsequent host tests above
  exercised cgroups and native benchmarks. Pure scheduler tests alone do not
  establish kernel enforcement or actual daemon-process restart behavior.
- NVIDIA driver access was unavailable in the sandbox; later approved host
  checks verified actual GPU kernels as recorded above.
- SuperLU_MT compilation and PETSc execution were not tested; native host checks
  reused a built bridge. At the time of this earlier inspection, PETSc was on
  upstream `feature/iterative-solvers`, not main.

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
