# Runwarden

Runwarden (`rmgr`) queues experiments on a single Linux workstation—for one
agent submitting many jobs or several agents sharing resources. It reserves
physical CPU cores, RAM, and optional access to one GPU. Systemd enforces CPU
assignments, memory limits, execution timeouts, and process-tree cancellation.
GPU reservations are cooperative.

**Requirements:** Linux with cgroup v2 · systemd 254+ · Python 3.11+

**Status:** 0.4 development (`0.4.0.dev1`). No Python runtime dependencies.
Validate enforcement on each target host; see the [validation evidence](docs/validation.md).

**On this page**

- [Install and verify](#install-and-verify)
- [Point an agent at Runwarden](#point-an-agent-at-runwarden)
- [Submit your first job](#submit-your-first-job)
- [Monitor jobs and read output](#monitor-jobs-and-read-output)
- [Configure resources](#configure-resources)
- [Receipts, cancellation, and recovery](#receipts-cancellation-and-recovery)
- [Troubleshooting and upgrades](#troubleshooting-and-upgrades)
- [Limits and security](#limits-and-security)
- [Examples and development](#examples-and-development)

## Install and verify

For an existing installation, follow [upgrade or repair](docs/host-setup.md#upgrade-or-repair).
For a first installation:

**1. Get the source.** If you already have a checkout, use that directory.

```bash
git clone https://github.com/syadegari/runwarden.git
cd runwarden
```

**2. Check host delegation** from a normal host terminal:

```bash
systemctl show "user@$(id -u).service" -p Delegate -p DelegateControllers
```

If `cpuset` or `memory` is missing, complete the
[host setup steps](docs/host-setup.md#missing-cpu-or-memory-delegation) before
continuing. The `cpu` and `cpuset` controllers are separate. If the user bus is
unavailable, first check `systemctl --user status` outside any restrictive sandbox.
**Before any repair that requires a reboot, finish/cancel managed jobs and save your work.**

**3. Install and verify enforcement.** On fresh Ubuntu 24.04, install missing
packaging tools with `sudo apt-get install pipx python3-venv`. Then:

```bash
pipx ensurepath &&
export PATH="${PIPX_BIN_DIR:-$HOME/.local/bin}:$PATH" &&
pipx install . &&
rmgr init &&
rmgr doctor &&
rmgr service install &&
rmgr health --wait 30 --json
```

`ensurepath` configures future shells; the export updates this one. `init` creates
configuration, while `doctor` tests actual enforcement. **Stop if the probe fails**;
follow its hint or run `rmgr manual host-setup`. `health --wait 30` waits for startup
and exits nonzero if readiness is not reached.

<details>
<summary>Install with a virtual environment instead</summary>

```bash
python3 -m venv .venv && .venv/bin/python -m pip install . &&
.venv/bin/rmgr init &&
.venv/bin/rmgr doctor &&
.venv/bin/rmgr service install &&
.venv/bin/rmgr health --wait 30 --json
```

Use `.venv/bin/rmgr` in the commands below, or activate the environment.
Keep the environment at its original path: the service records its interpreter's
absolute path. Jobs can use a different Python environment from the manager.

</details>

Use one shared daemon and state directory for all agents. The default state
location is `$XDG_STATE_HOME/rmgr`, or `~/.local/state/rmgr`.
For startup at boot and persistence after logout, see
[user lingering](docs/host-setup.md#startup-at-boot-and-after-logout).

## Point an agent at Runwarden

Tell the agent:

> Run `rmgr instructions` and follow the printed instructions before submitting experiments.

This works offline. `rmgr manual all` provides the full guide, and
`rmgr COMMAND --help` shows exact syntax. Agents can also use
`rmgr describe --json` for a machine-readable command catalog.

## Submit your first job

This example needs only a shell, one configured core, and 64 MiB of available
reservation capacity. Run it from your experiment directory:

```bash
job=$(rmgr submit --id-only --receipt ./demo.receipt.json \
  --cores 1 --memory 64M --timeout 30 -- bash -c 'sleep 2 && echo "finished"') &&
rmgr wait "$job" --timeout 60 &&
rmgr logs "$job" --text
```

Use a new receipt path for each new experiment. Submission returns immediately;
`wait` waits for completion, and `logs` retrieves output. A successful submission
does not mean the experiment succeeded.

Everything before `--` configures Runwarden; everything after it is your program
and its arguments. Submit existing programs directly:

```bash
rmgr submit --receipt ./run-01.receipt.json --cores 2 --memory 1G \
  --timeout 600 -- python benchmark.py --workers 2
```

Replace the example program with yours and set its worker count to match the
reservation. Add `--exclusive` to run alone among managed jobs. Jobs have no
interactive stdin; shell operators such as `&&` require an explicit `bash -c`,
as in the first example. `program | rmgr` is not a submission interface.

<details>
<summary>Worker counts, Conda, and environment variables</summary>

`--cores` controls which physical cores a job may use, not how many threads it
creates. Executable names are resolved on the submitting client's PATH; activate
the intended environment or supply an absolute executable path.

OpenMP/BLAS thread defaults match the core reservation. Override them with
`--env OMP_NUM_THREADS=1`, etc., when needed. Jobs receive a small environment,
not the agent's complete environment. Explicit variables are stored in job history,
so do not put secrets in them. See [advanced submission](docs/usage.md#submission-and-environments)
for Conda, JSON specifications, and dependencies.

</details>

## Monitor jobs and read output

| Task | Command |
| --- | --- |
| Overview | `rmgr status` |
| Refresh automatically | `rmgr status --watch 2` |
| Active and queued jobs | `rmgr list --text --active` |
| Finished jobs, all pages | `rmgr list --text --finished --all` |
| Job details and output paths | `rmgr inspect JOB_ID --text` |
| Execution directory | `rmgr inspect JOB_ID --cwd-only` |
| Program stdout / stderr | `rmgr logs JOB_ID --text` / `rmgr logs JOB_ID --stream stderr --text` |
| Follow recent stdout | `rmgr logs JOB_ID --tail 4096 --follow --timeout 20 --text` |

`status` defaults to text. Other data commands default to JSON; use `--json` for
compact JSON or `--text` for supported human-readable views. Memory figures are
**reservations, not measured RAM use**. Add `--wide` to text `list` or `status`
for complete commands, reasons, and execution directories.

Jobs write `rmgr-JOB_ID.stdout.log` and `rmgr-JOB_ID.stderr.log` in their working
directory (the submission directory unless `--cwd` is set). `rmgr logs` reads
those files; SQLite stores metadata and paths, not another copy of the output.
**Existing output files are never overwritten.** Keep files at their recorded
locations and manage retention yourself.

<details>
<summary>Output paths, missing files, and launch failures</summary>

Set `--stdout stdout.log --stderr stderr.log` for custom names in a separate run
directory. Parent directories must exist; existing targets cause launch failure.
Missing or moved files cannot be retrieved through `logs`. Launcher diagnostics
are kept separately from program stderr and appear in the job's `reason` or the
service journal. See [output details](docs/usage.md#output-files-and-manager-records).

</details>

<details>
<summary>Pagination, snapshots, and log-reading limits</summary>

`list` shows up to 100 rows by default; `--all` fetches remaining pages. Combine
`--state` filters or use `--active` / `--finished`; `--cwd PATH` filters by execution
directory. Log reads are bounded: `--tail 4096` means bytes, not lines.
`--follow` also has time and byte limits. A finished log read does not establish
job success. See [monitoring details](docs/usage.md#monitoring-and-pagination)
for snapshots, cursors, and older-daemon behavior.

</details>

## Configure resources

`rmgr init` generates CPU IDs and a memory budget. Its defaults select one
hardware thread per physical core and reserve 75% of physical RAM. Lower the
budget if other applications need more headroom.

To change capacity, pause submissions, finish/cancel managed work, stop the service,
edit the state directory's `config.json`, then start the service. Queued requests
are not resized when capacity changes. This is an illustrative configuration:

```json
{
  "cpu_ids": [0, 1, 2, 3],
  "memory_bytes": 8589934592,
  "gpu_count": 0
}
```

Use the CPU IDs generated for your machine. Set `gpu_count` to `1` only when
GPU 0 is ready, then use `--gpu` on submission to request it.
GPU reservations coordinate cooperating jobs; they do not enforce
device isolation or partition GPU memory. `gpu_device_isolation: false` does not
mean the GPU is inaccessible. See [GPU health fields](docs/usage.md#gpu-health-fields).

## Receipts, cancellation, and recovery

Every job has a public ID and a private control token saved in its receipt.
All agents can inspect jobs; ordinary cancellation and retries require the token.
Keep receipts private, outside source control, and in a durable location. Preserve
receipt paths in agent handoff notes. Existing receipts are never overwritten.

```bash
rmgr lookup --receipt ./run-01.receipt.json --json  # Find without submitting
rmgr retry --receipt ./run-01.receipt.json         # Recover an uncertain submission
rmgr cancel JOB_ID --receipt ./run-01.receipt.json
```

After a lost submission response, retry the **same receipt** to avoid duplicates.
An accepted retry returns the original job, even if that job failed. A deliberate
rerun needs a new receipt/request ID. There is no API to edit a submitted job.

Keys prevent accidental API interference, not unrestricted access by processes
sharing a Linux account. A human operator has a separate root-authorized
[admin-cancel command](docs/host-setup.md#operator-cancellation-without-a-job-key).
For detailed contracts, see `rmgr manual ownership` and
[agent discovery and recovery](docs/usage.md#agent-discovery-and-recovery).

## Troubleshooting and upgrades

| Check | Command / action |
| --- | --- |
| Daemon readiness | `rmgr health --json` |
| Service state | `rmgr service status` |
| Why a job is waiting or failed | `rmgr inspect JOB_ID --text` |
| Scheduler diagnostics | `journalctl --user -u rmgr.service -n 100 --no-pager` |
| Enforcement before startup | `rmgr doctor` — only with the daemon and jobs stopped |

Follow the [host setup guide](docs/host-setup.md) for delegation failures, PATH,
service persistence, upgrades, and older-version compatibility. Do not replace
package files while job launchers are starting.

Jobs run in independent systemd units: restarting the scheduler does not cancel
them. Reboot interrupts running jobs; queued jobs remain durable. Uncertain launches
are never automatically replayed. See [recovery and exit codes](docs/usage.md#recovery-states-and-exit-codes).

## Limits and security

- One shared daemon on one Linux workstation; no cluster scheduler or network server.
- Native local workloads must stay in their job cgroup. Remote MPI ranks and jobs
  launched through external daemons do not automatically inherit these limits.
- `--exclusive` applies only to managed jobs; it does not stop desktop applications.
- Runwarden is not a security sandbox. Submission permits execution as the daemon's
  user, and unrestricted same-user programs can bypass its cooperative controls.
- Inputs and executables are not snapshotted. Preserve each experiment's checkout
  and inputs until completion; source revisions are not discovered automatically.

## Examples and development

- [Demo jobs](docs/demo-jobs.md): CPU/GPU simulations and native FE benchmarks.
- [Advanced usage](docs/usage.md): submission, dependencies, monitoring, and API contracts.
- [Development guide](docs/development.md): source client, isolated preview, and tests.
- [Validation evidence](docs/validation.md): dated checks and their limitations.
- [Development permissions](docs/development-permissions.md): agent and host-access boundaries.
- [Historical code/documentation audit](docs/code-documentation-audit.md): the 0.3 review.
