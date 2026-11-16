# Advanced usage

For installation and everyday commands, start with the [README](../README.md).
The installed equivalents are available offline through `rmgr manual all` and
`rmgr describe --json`. These examples describe the 0.4 development client;
check the actual daemon's capabilities before using newer API features.

- [Submission and environments](#submission-and-environments)
- [Dependencies and scheduling](#dependencies-and-scheduling)
- [Output files and manager records](#output-files-and-manager-records)
- [Monitoring and pagination](#monitoring-and-pagination)
- [Agent discovery and recovery](#agent-discovery-and-recovery)
- [Control tokens](#control-tokens)
- [Recovery, states, and exit codes](#recovery-states-and-exit-codes)
- [GPU health fields](#gpu-health-fields)

## Submission and environments

Run programs through `rmgr submit OPTIONS -- PROGRAM ARGUMENTS`. `--cores`
reserves physical cores; configure the program's worker count separately.
`--exclusive` prevents overlap with other managed jobs, not unmanaged workloads.
See the [FE and GPU demos](demo-jobs.md) for complete application examples.

Executable names are resolved on the submitting client. For Conda, either
activate the intended environment first or pass its absolute Python path:

```bash
conda run -n py3.14 python -c 'import sys; print(sys.executable)'
# Use that executable in the job command.
```

OpenMP/BLAS thread defaults equal the allocated core count. Override them with
`--env OMP_NUM_THREADS=1`, etc., when workers each need one thread. Jobs get a
small environment, not a copy of the agent's environment. Set any required
library paths or application variables explicitly. Explicit environment values
are saved in job history, so avoid putting secrets in them.

For structured submission, `rmgr submit --receipt ./job.receipt.json --spec job.json --request-id stable-key`
accepts fields `argv`, `cwd`, `cores`, `memory_bytes`, `gpu`, `exclusive`,
`timeout_seconds`, `after_success`, `env`, `stdout`, and `stderr`. In JSON, the executable and cwd
must be absolute. Unknown fields and impossible requests are rejected.
With `--spec`, put all job settings in the JSON file; command-line job options
are rejected if explicitly supplied alongside it. `--receipt` and `--request-id`
still apply to submission.

## Dependencies and scheduling

```bash
a=$(rmgr submit --id-only --receipt ./a.receipt.json --cores 8 -- python A.py)
rmgr submit --receipt ./b.receipt.json --cores 8 --after-success "$a" -- python B.py
rmgr submit --receipt ./c.receipt.json --cores 8 --after-success "$a" -- python C.py
```

B and C become eligible after A succeeds. They may overlap if resources fit;
their start times are not synchronized. Failed or cancelled prerequisites block
dependent jobs permanently. Dependencies must name existing jobs, preventing
cycles. Ready jobs use FIFO admission; a waiting ready job stops later ready
jobs from passing it. This prevents starvation but can leave resources idle.

## Output files and manager records

Each new job writes these files in its `--cwd` (default: the
directory from which you submit):

```text
rmgr-JOB_ID.stdout.log
rmgr-JOB_ID.stderr.log
```

Use explicit names when each experiment has its own run directory:

```bash
rmgr submit --receipt ./run-02.receipt.json --cwd /path/to/experiment \
  --stdout stdout.log --stderr stderr.log --cores 2 --memory 1G \
  -- /path/to/python -m fe_solver solve run.toml --solver-backend superlu_mt --num-threads 2
```

Relative output paths are relative to the job's cwd; absolute paths also work.
Parent directories must already exist. The launcher creates private regular
files (mode 0600) when the job starts. Existing files, symlinks, and output-path
collisions cause launch failure rather than overwriting/appending another run.
A failed launch can leave empty files it created before the failure.

Submission and `inspect` return `stdout_path` and `stderr_path`. `rmgr logs`
reads those files; it also returns `path` and `available`. Before creation, or
if a file/directory was removed or moved, it returns `available: false` and empty
text. Replacing a new job's output file produces `LOG_UNAVAILABLE`, so a different
file is not silently presented as that job's output. Keep output files at their
recorded locations if you want to retrieve them through the API.

SQLite stores job metadata and output paths, not program output. The central
`jobs/JOB_ID/` directory keeps the launch manifest, output-file identity record,
and any `launch-error.txt`. Launcher errors are exposed in the job's `reason`
and the journal, not mixed into program stderr. Scheduler messages are in
`journalctl --user -u rmgr.service`. Retention of both records and output files
is manual.

## Monitoring and pagination

```bash
rmgr status                           # Capacity, readiness, and all active/queued jobs
rmgr status --watch 2                  # Refresh until Ctrl+C
rmgr list --text --active              # Queued, starting, running, cancelling
rmgr list --text --finished            # All terminal states, including blocked/interrupted
rmgr list --text --state running --state queued
rmgr list --text --finished --all --wide
rmgr inspect JOB_ID --text             # Details, current capacity, and output paths
rmgr resources --text
rmgr health --text
rmgr logs JOB_ID --text
rmgr logs JOB_ID --stream stderr --text
```

`status` defaults to text; `--json` returns an object with `observed_at`, `health`,
`resources`, and `jobs`. Other commands keep their existing JSON defaults and
accept `--text` explicitly. `--json` and `--text` cannot be combined.
Basic text views need no external formatting tools and can query a 0.2 daemon.
Snapshot pagination and log tail require a 0.4 daemon; see
[version compatibility](host-setup.md#compatibility-with-older-installations).
Text views query only public job information; they do not read receipts or control tokens.

Memory figures are **reservations, not measured RAM usage**. Tables show full
job IDs, requested CPUs/memory, execution runtime, exit code, command, and reason.
Jobs that never started have no runtime; finished runtimes stop at the end time.
Use `--wide` on text `list` or `status` to show complete command lines, reasons,
and execution directories. `inspect JOB_ID --cwd-only` prints the directory alone;
JSON inspect/list already include it as `spec.cwd`. `list --cwd PATH` selects
jobs from that exact directory. This is the configured execution directory, not
a separately recorded shell invocation directory or the process's current cwd.
`inspect --text` shows the full command and recorded reason; it does not interpret
application stderr. Its available resources are current, not historical.

`list` defaults to 100 rows, oldest first; the footer reports additional pages.
`--all` fetches every remaining match from `--offset`. `--limit` remains a page
size of 1–100. Repeated `--state` values are combined before applying the offset
and limit. Choose only one of `--state`, `--active`, or `--finished`.
`status` includes every active/queued job, across pages.
With a 0.4 daemon, status is a consistent snapshot and list pages use an immutable
snapshot. Continue a page with `--snapshot ID --offset NEXT_OFFSET`, or let
`--all` do it. Snapshots expire after 120 seconds, on daemon restart, or when
evicted from the bounded cache (16 snapshots / 32 MiB total). Expired snapshots
return `SNAPSHOT_EXPIRED`; start a new listing explicitly. Narrow the state/cwd
filters if one snapshot exceeds 32 MiB. Legacy daemons use separate live queries
and report `consistency: best_effort_legacy`; jobs may change between pages.

`--watch` takes a positive finite interval in seconds, measured between completed
refreshes. Text watch redraws a terminal, or appends snapshots when redirected;
`--json --watch 2` emits one JSON object per line. Ctrl+C exits with code 130.
Degraded health is displayed and retried during watch; connection errors stop
monitoring with exit code 2. A one-shot degraded `status` also returns 2.

`logs --text` writes the selected chunk directly to stdout without adding a
newline. Offset/limit remain byte-based. More-output and missing-file notices
go to stderr, as do CLI/API errors (still JSON). Use `--tail 4096` to start at
the last 4096 **bytes** (requires a 0.4 daemon). `--follow` polls until a terminal
job's remaining output is read, a deadline expires, or its byte budget is reached.
Following alone uses the ordinary log API; combining it with `--tail` requires
the newer daemon. Defaults are `--timeout 30 --interval 1 --max-bytes 1048576`. Follow output is
raw text with `--text`, otherwise one JSON chunk per line. Completion of a log
read is not evidence of job success; use `wait` or `inspect` for that.

## Agent discovery and recovery

```bash
rmgr describe --json                   # Offline command catalog and contracts
rmgr describe submit --json            # One command, including its arguments
rmgr health --json                     # Actual daemon protocol_version/features
rmgr validate --cores 8 --memory 16G -- /path/to/python -m fe_solver solve ...
rmgr lookup --receipt ./run.receipt.json --json
rmgr logs JOB_ID --stream stderr --tail 4096 --follow --timeout 20 --text
```

`describe` is generated from CLI argument definitions and JobSpec fields. It
reports authorization, output-field names, defaults, limits, states, and exit
codes. `instructions` remains the concise agent entry point. Client version is
not a guarantee of daemon features: inspect health's `protocol_version` and
`features`; their absence means legacy/unknown. Validation, tail, snapshots,
structured queue details, and operator cancellation require a 0.4 daemon.

`validate` checks the resolved specification, executable, dependencies, and output
paths against current configured capacity. It reports the effective environment
and warnings but writes no receipt, reserves nothing, and cannot guarantee
future launch. `lookup` reads only the explicitly supplied receipt and finds its
request ID without submitting or exposing its token; `found: false` means no
accepted job was found. It also works with older daemons by searching public history.

Submission errors include `acceptance`, `recovery`, `request_id`, and `receipt`
when a receipt has been saved and submission attempted. A definite rejection
instructs the caller to correct the request and use a new receipt. An uncertain
outcome requires retrying the same receipt; never create a second job to recover
an uncertain submission. Local errors before submission may lack these fields.
Existing accepted retries still return the original job, even if it failed.

Queued jobs expose `reason_details` with a stable code and relevant blocking IDs
or requested/available resource amounts. Codes include `CPU_RESERVATION`,
`MEMORY_RESERVATION`, `GPU_RESERVATION`, `EXCLUSIVE_RUNNING`, `EXCLUSIVE_DRAIN`,
`DEPENDENCIES`, `DEPENDENCY_FAILED`, and `FIFO_BARRIER`. Older jobs may have null
details until their next scheduling decision. Existing JSON fields remain;
new metadata is additive, except that printing a control token is now opt-in.

## Control tokens

Submission returns a public `id` and the receipt path. Starting with the 0.4
client, the private `control_token` stays in the receipt unless `--show-token`
is explicitly requested on submit/retry. Ordinary cancellation requires that token, supplied through `--receipt PATH` or
`--token-stdin`. Tokens are never included in public queries or job manifests;
the daemon stores only their hashes. Read-only queries remain shared.

Keep receipts private and out of source control. They are created with mode
0600 and are never found/reused automatically on another agent's behalf. The
receipt is the recovery path after a lost response or conversation restart.
There is no ordinary-user tokenless force-cancel or lost-key recovery API.
The 0.4 daemon adds a separate root-authorized `admin-cancel` operation with an
audit reason; see [operator cancellation](host-setup.md#operator-cancellation-without-a-job-key).
This guards against accidental API interference, not unrestricted same-user
filesystem access or direct process/service control. See `rmgr manual ownership`.

## Recovery, states, and exit codes

```bash
rmgr service status
rmgr health --json
rmgr resources --json
rmgr service restart
journalctl --user -u rmgr.service -n 100
```

`service status` reports systemd state. `health` checks the API, database, and
systemd connection. `doctor` additionally exercises real enforcement. A PID file
or existing socket is never treated as proof of health.

Jobs run in independent systemd units. Stopping/restarting the scheduler does
not cancel them. On recovery it reconciles those units with persisted reservations
before scheduling. Missing units become `interrupted`; uncertain launches are
never automatically replayed. Backend query failures stop admission and retain
reservations. Reboot interrupts running jobs, while queued jobs remain durable.

Final states are `succeeded`, `failed`, `cancelled`, `timed_out`, `oom`, `blocked`,
and `interrupted`. CLI exit codes: 0 success, 2 API/configuration/service error,
3 wait deadline reached (job still running/queued), 4 job completed unsuccessfully.
Argparse usage errors also return 2. `inspect` returns 0 for an existing job
regardless of that job's outcome.

A UTF-8 character split at a byte cursor can be replaced in displayed log text;
the stored log bytes remain unchanged. `instructions`, `manual`, help,
`service render`, and submission with `--id-only` produce plain text.

## GPU health fields

Both `health` and a successful `doctor` report these GPU fields under `enforcement`:

- `gpu_device_isolation: false`: GPU device access is not enforced; this does
  not mean the GPU is inaccessible. This replaces the misleading
  `gpu_device_access` field in older versions.
- `gpu_reservation`: whether GPU scheduling is enabled (`gpu_count=1`).
- `gpu_availability: "not_checked"`: neither check tests GPU hardware or drivers.
- `gpu_note`: explains these limitations and whether reservations are enabled.

Upgrade both the package and daemon to change the reported fields. Older cached
enforcement evidence is translated on startup; it does not establish GPU access.
