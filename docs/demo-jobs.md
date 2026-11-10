# GPU simulations and 16-thread FE benchmarks

For ordinary jobs, submit your program directly:

```bash
rmgr submit --receipt ./solve-01.receipt.json --cores 16 --memory 8G \
  --timeout 600 --cwd /path/to/fe_solver \
  -- /path/to/envs/py3.14/bin/python -m fe_solver solve /path/to/run.toml \
  --solver-backend superlu_mt --num-threads 16
```

Configure the FE library and the deck's separate output directory as for a
normal solver invocation. The `--` separates manager options from target
arguments. No Python submission script or JSON file is necessary. Submission
returns immediately; use the monitoring commands below to follow the run.

The helper below is optional convenience for submitting the paired demos.
Run its commands from this repository's root. It creates a fresh
private directory under `build/demo-runs/` with receipts and a `jobs.json` index.
It prints the job IDs immediately; jobs continue after the command exits.

## Minimal scheduling check

To check that two jobs requesting all 16 cores queue one after the other, each
job can simply sleep for ten seconds. No build or workload script is needed:

```bash
receipts=$(mktemp -d)
for job in 1 2; do
  rmgr submit --id-only --receipt "$receipts/sleep-$job.receipt.json" \
    --cores 16 --memory 64M --timeout 30 -- sleep 10
done
```

With 16 configured cores, the second job waits until the first releases its
reservation. Sleeping reserves the cores without consuming them; the compute
examples below also exercise actual CPU or GPU work. Keep the printed IDs and
the receipt directory for monitoring and cancellation.

## Two native FE jobs

Use an existing FE checkout, its built SuperLU_MT bridge, and the `py3.14`
interpreter. The library may live in another build directory if its ABI matches
the checkout's `native/superlu_mt_backend.py`.

```bash
python3 examples/submit_demo_jobs.py fe \
  --fe-repo /absolute/path/to/fe_solver \
  --fe-lib /absolute/path/to/build/native/libsuperlu_mt_bridge.so \
  --python /absolute/path/to/envs/py3.14/bin/python
```

Both jobs read `native/kkt_debug_1.h5`, group `t_n=0.02/iteration=2`:

1. `fe-compare`: `benchmark_superlu_mt.py --threads 16`, comparing the native
   factorization and solve to SciPy, including residual and solution differences.
2. `fe-blocking`: `benchmark_superlu_mt_blocking.py --threads 16`, comparing the
   default blocking configuration with `max_supernode=100`, one repetition each.
   Its structured output is saved as `blocking-results.json` beside the receipts.

Each reserves 16 physical cores and 8 GiB, with `--exclusive` and a 180-second
timeout. They run one at a time. SuperLU_MT creates its own 16 pthread workers;
BLAS/OpenMP pools are capped at one to avoid nested oversubscription. The serial
SciPy comparison and setup phases will not use all 16 cores. A 16-thread
reservation does not imply every phase saturates every core.

The native project documents intermittent SuperLU_MT numerical errors. Its
comparison script can print `match=False` while exiting successfully: inspect
that column and residuals, not just the scheduler's `succeeded` state. The
blocking benchmark measures factorization timing; it does not validate solutions.
These two bounded runs are integration checks, not a numerical reliability study
or a statistically meaningful performance comparison.

## Two GPU jobs

The CUDA examples actually execute kernels on GPU 0:

- `gpu-heat`: explicit periodic 1D heat diffusion over 1,048,576 cells.
- `gpu-particles`: 1,048,576 independent harmonic particles, advanced with a
  symplectic Euler step.

Both log progress approximately once per second and check final values for
finiteness and conservation. Device arrays occupy 12 MiB; the CUDA context adds
driver overhead. They default to 30 seconds each and request one CPU core,
1 GiB of host RAM, and the single GPU reservation, so they run sequentially.

Build with GCC/OpenMP and CUDA `nvcc` (the GPU build targets compute capability
7.5 and includes PTX for newer devices):

```bash
bash examples/mock_simulations/build.sh
```

If `rmgr resources` shows `gpu_count: 0`, wait for the queue to empty, stop the
service with `rmgr service stop`, and change only `gpu_count` to `1` in your
state directory's `config.json` (normally `~/.local/state/rmgr/config.json`).
Then run `rmgr service start` and confirm `rmgr resources` reports `gpu_count: 1`.
This enables cooperative scheduling; it does not enable GPU device isolation.

```bash
python3 examples/submit_demo_jobs.py gpu
```

For longer demonstrations, add `--seconds 60`. The original CPU mocks are also
available with `python3 examples/submit_demo_jobs.py cpu`: heat diffusion and
particle motion, each using 16 real OpenMP threads and requesting all 16 cores.
The FE jobs above are the more representative native interoperability check.

## Monitor and read logs

Use the printed IDs, or recover them from the run directory's `jobs.json`.

```bash
watch -n 1 'rmgr list'
rmgr inspect JOB_ID
rmgr resources
rmgr wait JOB_ID --timeout 180
```

`wait` exits 0 on success, 3 if its waiting deadline expires, and 4 if the job
finishes unsuccessfully. Its deadline does not cancel the job. Use `htop`
(show threads) for CPU activity, or `watch -n 1 nvidia-smi` for GPU activity.

Logs remain available after completion:

```bash
rmgr logs JOB_ID --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["text"], end="")'
rmgr logs JOB_ID --stream stderr --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["text"], end="")'
```

The API defaults to 16 KiB per response. For longer logs, use `--offset` with
the previous response's `next_offset`, or read the complete files directly:

```bash
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/rmgr"
tail -F "$state_dir/jobs/JOB_ID/stdout.log" "$state_dir/jobs/JOB_ID/stderr.log"
# After completion (also works with the daemon stopped):
less "$state_dir/jobs/JOB_ID/stdout.log"
cat "$state_dir/jobs/JOB_ID/stderr.log"
```

Replace `JOB_ID` in paths as well as commands. For a custom state directory,
use its path and pass `rmgr --home /that/path` to CLI commands. `tail -F` keeps
waiting after a job finishes; Ctrl-C stops the viewer, not the job.

To cancel your own job, use its saved receipt:

```bash
rmgr cancel JOB_ID --receipt /path/from/jobs.json
```
