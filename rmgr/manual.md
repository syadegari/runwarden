# rmgr manual

## overview

rmgr schedules native jobs for cooperating agents on one Linux workstation.
It reserves physical cores and RAM, offers exclusive execution among managed
jobs, and optionally serializes access to one GPU. Use the same daemon/state
directory as the other agents. Do not start a second daemon to bypass a queue.

Start here:

    rmgr --version
    rmgr instructions
    rmgr health --json
    rmgr resources --json
    rmgr submit --help
    rmgr manual jobs
    rmgr manual ownership
    rmgr manual limits

This manual works offline without a daemon. Topics are overview, instructions, jobs,
ownership, limits, and operations. Use `rmgr manual TOPIC` for a short section,
`rmgr manual all` for the complete guide, or add `--json` for a text envelope
with version and topic information. No pager or interactive prompts are used.
`rmgr instructions` prints the instructions topic directly to stdout as readable
text, ready to pass to an agent. `rmgr instructions --json` provides the same
content in the manual's version/topic/text envelope. Neither command contacts
the daemon, reads receipts, or initializes state.

All agents should read ownership and limits before launching experiments.
`--help` describes exact command syntax; this manual explains workflow and
semantics. Runtime readiness and capacity come from health/resources, not the
manual. The tool is experimental until the live host tests pass on your machine.

Pass a nondefault state directory before the command:

    rmgr --home /shared/private/rmgr-state health --json

CLI result output goes to stdout. Existing data commands use indented JSON by
default; list, inspect, resources, health, and logs also accept --text.
The combined `status` view defaults to text and accepts --json.
CLI/API exceptions are JSON on stderr; doctor, health, and status print their
diagnostic reports to stdout even when they exit 2.
Argument-parser usage errors are ordinary text. `--json` produces compact JSON
for data commands. Do not parse human formatting or interpret an exit code of
zero from `submit` as evidence that the experiment has finished.
Submitted program output is captured in files rather than streamed by submit.

## instructions

Use Runwarden (rmgr) for native experiments on this workstation. Read
`rmgr manual ownership` and `rmgr manual limits` before submitting work;
use `rmgr manual jobs` and command-specific `--help` for syntax and details.
Use `rmgr describe --json` (or `describe COMMAND --json`) for machine-readable
arguments, defaults, limits, output fields, and authorization requirements.
Check health's protocol_version/features before using newer daemon features;
the installed client version alone does not establish daemon capabilities.

Use the shared daemon/state directory. Check `rmgr health --json` and
`rmgr resources --json` before submission. Do not start a separate daemon to
bypass a queue or run an experiment outside the manager when admission fails.
When supported, use `rmgr validate JOB_OPTIONS -- PROGRAM ARGUMENTS` to check
the effective spec/environment before submission; it creates no receipt or
reservation and does not guarantee future launch.

Submit the target command directly; no wrapper script is required:

    rmgr submit --id-only --receipt /private/persistent/run.receipt.json \
      --cores N --memory SIZE --timeout SECONDS -- PROGRAM ARGUMENTS

Choose resources to fit the available configuration and set the program's worker
counts explicitly. Use the intended environment's executable. Use separate run
directories and preserve inputs until jobs finish. Submission returns immediately;
use `rmgr wait JOB_ID`, `rmgr inspect JOB_ID`, and `rmgr logs JOB_ID` to monitor it.
Program stdout/stderr go to files in job cwd by default. Inspect returns their
paths. Optional --stdout/--stderr select new files; existing targets are not
overwritten. The manager keeps its own records centrally.

A public job ID is NOT the secret control_token. List/inspect expose job IDs,
not control keys. Submission generates a random key and saves it in your receipt
before contacting the daemon. The 0.4 client keeps the key out of ordinary
submit/retry output; only explicit --show-token prints it. The target program is not given
the key. Read-only queries are shared; cancelling requires the matching key.

Use a new receipt path for each new job. Keep receipts private, persistent, and
out of version control. Record your job IDs and receipt paths in your own handoff
notes across conversation restarts; do not copy the keys into shared notes.
The manager cannot remember those paths on an agent's behalf. A temporary
directory that may be cleaned up is unsuitable for long-term recovery.

For read-only recovery, `rmgr lookup --receipt PATH --json` returns found/job
without submitting or printing the key. After a lost submission response,
use `rmgr retry --id-only --receipt PATH` to safely complete the same request.
An already-accepted request returns its original job ID without executing again;
if it was never accepted, retry submits the saved request. To cancel, use
`rmgr cancel JOB_ID --receipt PATH`. Only cancel jobs you submitted or for which
the user explicitly delegated authority and supplied the matching receipt.
Do not search for/reuse another agent's receipts or bypass the API with direct
process/service control. There is no job-editing API or lost-key recovery API.
Do not use sudo or admin-cancel as an agent cancellation fallback. Only the
human operator should use that separate privileged path.

Use bounded waits and log reads: `rmgr logs JOB_ID --stream stderr --tail 4096
--follow --timeout 20 --text` reads a bounded tail and follows for up to 20 seconds.
Inspect/list JSON includes spec.cwd; `inspect JOB_ID --cwd-only` prints the
execution directory, and `list --text --wide` includes directory columns.

The --exclusive flag means running alone among managed jobs; it does not grant
ownership or replace the control key. Keys prevent accidental API interference,
not unrestricted access by other processes under the same Linux account.

## jobs

The normal interface is a command prefix:

    rmgr submit MANAGER_OPTIONS -- PROGRAM PROGRAM_ARGUMENTS

No wrapper script or JSON file is required. Everything after `--` is passed as
the target program's arguments, including its own flags. The manager queues
and launches that program when resources become available. `program | rmgr`
is not supported: the shell would start the program before admission and pipe
its output instead of submitting its command.
Jobs have no interactive stdin. To use shell operators, submit the shell itself:

    rmgr submit --receipt ./sleep.receipt.json --cores 16 --memory 64M \
      -- bash -c 'sleep 10 && echo "finished"'

Choose a private receipt path unique to this submission and an experiment
directory that other jobs will not modify:

    rmgr submit --receipt ./experiment-01.receipt.json --cores 8 --memory 16G \
      --timeout 600 --exclusive -- /path/to/python /path/to/benchmark.py --workers 8

The CLI creates the receipt with mode 0600 before sending the request. The
submission response contains the public job `id` and receipt path. The private
control_token stays in that receipt unless --show-token is explicitly requested.
Keep the receipt outside tracked/shared job artifacts.
Receipt paths are never returned by job queries. Existing receipts are never
overwritten: use retry to repeat a submission, or a new path for a new run.

    rmgr inspect JOB_ID --json
    rmgr logs JOB_ID --stream stdout --offset 0 --limit 16384 --json
    rmgr logs JOB_ID --stream stderr --json
    rmgr wait JOB_ID --timeout 30 --json
    rmgr cancel JOB_ID --receipt ./experiment-01.receipt.json

New jobs write rmgr-JOB_ID.stdout.log and rmgr-JOB_ID.stderr.log in their cwd.
Use --stdout stdout.log --stderr stderr.log to choose filenames; relative paths
resolve against job cwd, and absolute paths are accepted. Parent directories
must exist. Output files are created at launch with mode 0600 and never overwrite
or append existing files. Use separate paths for stdout and stderr. A collision,
unwritable directory, or symlink causes launch failure; check inspect's reason.
Failed launches may leave newly created empty files. Retention is manual.

Submit/inspect/list expose stdout_path and stderr_path. The logs command reads
those files and returns path and available along with the text/cursors. Missing
files (including files not created yet or moved/deleted later) return available=false
and empty text. A replaced file or other read failure returns LOG_UNAVAILABLE.
Keep outputs at their recorded paths for API retrieval. The central state stores
metadata, launch diagnostics, and file identities, not new jobs' program output.
Launcher failures remain in the central launch-error.txt/job reason and journal;
they are not mixed into the target program's stderr.

Use each log response's `next_offset` for the next read; offsets/limits count
bytes. `more` means more bytes were available at that read, not that the job is
finished. Poll again later while the job runs. Default chunk size is 16 KiB;
maximum is 1 MiB. A split UTF-8 character may display as a replacement character.

If submission loses its response, reuse the receipt:

    rmgr retry --receipt ./experiment-01.receipt.json --json

Retry sends the original command, request ID, and token. It returns the same job
if already accepted, including if that job has finished. It does not rerun a
failed job. A deliberate rerun needs a new receipt/request ID. If receipt
creation itself fails, no request is sent. Do not edit a receipt to change a job.
Already-accepted retries still work if the old cwd disappeared, resource capacity
changed, or admission is degraded; genuinely new submissions are still validated.

To construct a dependency chain or branch:

    a=$(rmgr submit --id-only --receipt ./a.receipt.json --cores 8 -- /path/to/python A.py)
    rmgr submit --receipt ./b.receipt.json --cores 8 --after-success "$a" -- /path/to/python B.py
    rmgr submit --receipt ./c.receipt.json --cores 8 --after-success "$a" -- /path/to/python C.py

B and C become eligible after A succeeds; they are not guaranteed to start at
the same instant. Repeat --after-success for multiple prerequisites. Dependencies
must already exist. A failed/cancelled/interrupted prerequisite blocks its
dependents. Ready jobs use FIFO admission; later jobs may wait despite spare
resources to avoid starving the first ready job.

`rmgr submit --receipt PATH --spec job.json` accepts this specification shape:

    {
      "argv": ["/absolute/path/to/python", "benchmark.py"],
      "cwd": "/absolute/experiment/directory",
      "cores": 8,
      "memory_bytes": 17179869184,
      "gpu": false,
      "exclusive": true,
      "timeout_seconds": 600,
      "after_success": [],
      "env": {"OMP_NUM_THREADS": "8"},
      "stdout": "stdout.log",
      "stderr": "stderr.log"
    }

argv and cwd are required; stdout/stderr are optional (or null for defaults).
With --spec, put job settings in JSON: explicitly supplied command-line job
options are rejected, including options equal to defaults. Receipt and
request-id options still apply.
JSON executable/cwd paths must be absolute. CLI
executable names are resolved by the submitting client. Defaults: 1 physical
core, 1 GiB RAM, no GPU, nonexclusive, 3600 seconds, no dependencies or explicit
environment entries. Minimum RAM is 64 MiB; requests must fit configured
capacity. Specifications are limited to 64 KiB. Unknown fields are errors.
CLI memory suffixes K/M/G/T (also KiB/MiB/GiB/TiB) use powers of 1024.

Only explicitly supplied environment entries and a small base environment are
passed to a job. Thread defaults for OpenMP/BLAS equal the core reservation;
override with --env KEY=VALUE as needed. For Conda, submit the absolute path of
the intended environment's Python; no environment activation happens implicitly.
The manager's Python environment need not match the experiment's environment.

List pagination: `rmgr list --limit 100 --offset 0 --json`; follow next_offset
until it is null. The maximum page size is 100. `--all` fetches every remaining
match from --offset; --limit then controls the page size. --state filters job
state and can be repeated to combine states before pagination. --active includes
queued, starting, running, cancelling; --finished includes all terminal states.
Choose only one of --state, --active, or --finished. Results are oldest first.

Human-readable inspection (no jq or column required):

    rmgr status
    rmgr status --watch 2
    rmgr list --text --active
    rmgr list --text --finished --all
    rmgr list --text --state running --state queued
    rmgr list --text --wide
    rmgr inspect JOB_ID --text
    rmgr resources --text
    rmgr health --text
    rmgr logs JOB_ID --text
    rmgr logs JOB_ID --stream stderr --text

Tables show complete public IDs, states, requested CPUs/memory, runtime, exit
code, command, and reason. --wide (text list/status) shows full commands and
reasons. Jobs that never started show no runtime. Finished runtimes stop at
completion. A list footer indicates remaining pages. Inspection shows the full
command, timestamps, dependencies, allocation, current resource availability,
and output paths; it does not interpret application stderr.

Status combines health, resources, and every active/queued job. Memory numbers
are reservations, not actual usage. These client-only views also work with a
0.2 daemon and never read control receipts. With a 0.4 daemon, status is a
consistent snapshot; list uses frozen snapshots across pages. With a legacy
daemon, separate queries are used and marked best_effort_legacy; jobs can change
between queries/pages. Current capacity is not historical usage of a finished job.
List can filter exact execution directories with --cwd PATH. Continue frozen
pages with --snapshot ID --offset NEXT_OFFSET. Snapshots expire after 120 seconds,
on restart, or eviction from a 16-entry / 32-MiB-total cache. SNAPSHOT_EXPIRED
requires a new listing; narrow filters on SNAPSHOT_TOO_LARGE. --all handles pages.
Directory is the submitted execution cwd, not the caller's separately recorded
shell directory or a running process's subsequent chdir.

--watch takes a positive finite number of seconds between completed refreshes.
Text watch redraws a terminal or appends snapshots when redirected. Ctrl+C
returns 130. Degraded status continues refreshing; a connection error stops
watch with code 2. One-shot degraded status returns 2.
`status --json` returns observed_at, health, resources, and jobs;
`status --json --watch 2` emits one compact JSON object per line.
--text and --json cannot be combined. New metadata fields are additive; agents
should tolerate extra fields. Control token output now requires --show-token.

Logs --text prints only the requested chunk, without an extra newline; byte
offset/limit still apply. Notices about more output or an unavailable file go
to stderr. --tail BYTES starts at the last 1–1048576 bytes (requires log_tail in
health features). --follow drains output with --timeout 30, --interval 1, and
--max-bytes 1048576 defaults. It stops at terminal EOF, deadline, or byte budget.
Follow JSON emits one object per chunk; follow --text emits only program output.
Limit/deadline notices go to stderr and report the next offset. Successful log
retrieval does not imply a successful job. UTF-8 splits still use replacement characters.

`lookup --receipt PATH` reads only that receipt, returns found/request_id/job,
and never submits. `validate` shares submit's job options and --spec, returning
valid/spec/effective_environment/resources/warnings/reserved=false. It checks
current paths and configuration, not future file availability or admission.
It requires the validate daemon feature. `describe` is always offline.

After an attempted submission, structured errors include acceptance and recovery:
rejected/correct_request_use_new_receipt for definite request rejection, or
unknown/retry_same_receipt when acceptance cannot be established. Do not edit
receipts or create a new request to recover an uncertain outcome. Errors before
receipt creation or before sending may lack those fields.

Queue reason_details contains codes and relevant blocking_job_ids/resource
amounts. Codes: CPU_RESERVATION, MEMORY_RESERVATION, GPU_RESERVATION,
EXCLUSIVE_RUNNING, EXCLUSIVE_DRAIN, DEPENDENCIES, DEPENDENCY_FAILED, FIFO_BARRIER.
Older records may have null details. Launch preparation failures are recorded
as LAUNCH_PREPARATION_FAILED; storage uncertainty degrades admission while
keeping durable reservations until reconciliation.

Nonterminal states: queued, starting, running, cancelling.
Terminal states: succeeded, failed, cancelled, timed_out, oom, blocked,
interrupted. See `reason` and `exit_code` for diagnostics.

CLI exit codes: 0 means the command succeeded; 2 is an API/local/usage error;
3 means `wait` reached its client deadline while the job was unfinished;
4 means `wait` observed an unsuccessful terminal job. `inspect`, `list`, and
`cancel` report existing job data successfully regardless of the job's outcome.
A wait deadline never cancels the job. Job --timeout is enforced by systemd.

## ownership

A job ID is public. A control token is a separate random bearer credential:
anyone holding it can cancel that job through the API. No encryption is needed
for this mechanism. Never append the token to a public job ID or share it in
logs, issue trackers, or another job's command/environment.

The CLI generates 32 random bytes, encodes them as a URL-safe string, and saves
them in the private receipt before submission. The daemon stores only a SHA-256
digest. A lost response cannot lose the original token because retry uses the
receipt. The daemon never returns tokens/digests in list, inspect, wait, logs,
health, or runner manifests. The CLI adds the token to submit/retry output only
when explicitly requested with --show-token.

Cancellation requires the matching token, even for an already completed job.
Use `cancel JOB_ID --receipt PATH`. Alternatively, `--token-stdin` reads a raw
token from stdin, keeping it out of process command-line arguments. A receipt
for another job will not authorize cancellation. There is no ordinary-user
tokenless --force option, job editing operation, or API for recovering a lost token.
The separate operator-only admin-cancel path requires a root socket peer.

Only cancel work you submitted or for which control was explicitly delegated
to you. Having a token demonstrates authority to the daemon, not a reason to
cancel. Automatic timeout/dependency handling still works without client tokens.

Request IDs are public idempotency keys, not credentials. A duplicate submission
must match the original specification AND control token. The default request ID
is random. If setting --request-id yourself, use an experiment/agent-specific
prefix; another caller's matching key will cause an error, not grant control.

Read operations remain shared among clients of the same Linux account. Job
commands, explicit environment, status and logs are not confidential. Protect
secrets separately. Files created by the application are not isolated by rmgr.

This mechanism protects against accidental cross-agent API actions, not hostile
agents with unrestricted access to the same Linux account. Such agents can
read each other's files, signal processes directly, or control the user service.
Do not stop/restart the shared daemon, alter its database, or call systemctl to
bypass a denied cancellation. Service/configuration changes are operator tasks.

Upgrading from 0.1 adds a nullable token-hash column without replacing job
history. Old jobs have no token and cannot use ordinary token-authorized cancellation.
Finish/cancel old jobs with 0.1 before upgrading. Legacy history remains readable;
there is no automatic adoption or creation of credentials for somebody else's job.

Version 0.3 adds persisted output paths. Existing jobs and receipts retain their
old behavior: even pre-upgrade queued jobs keep central stdout.log/stderr.log
paths. New submissions use working-directory outputs. No historical files are
moved. Stop the scheduler, back up state, upgrade the package, and restart it.
Do not replace package files while a launcher is starting.

For direct local API clients: submit requests contain op="submit", request_id,
spec, and control_token. Generate/save a token before calling. Cancel requests
contain op="cancel", job_id, and control_token. Send one JSON line to the
private control.sock in the daemon state directory. Responses contain either
{"ok":true,"result":...} or {"ok":false,"error":{"code":...,"message":...}}.
Submit/retry API responses contain public job data; the CLI already possesses
the secret and adds it locally to its own submission output.

## limits

- One shared Linux-user daemon, native local execution, Python 3.11+, systemd
  254+, and cgroup v2. Access to its socket grants job execution as that user
  outside the submitting client's sandbox. It is not a security sandbox.
- Each allocated physical core uses one logical CPU. Jobs cannot share SMT
  siblings through this manager. Discovery handles multiple sockets, but
  allocation currently uses a flat CPU list; there is no socket/NUMA/cache
  placement policy or per-thread pinning. Configure OpenMP/MPI binding yourself.
- A core reservation is not a worker/thread count. Oversubscribing the assigned
  CPUs is still possible; set application workers and library threads explicitly.
- RAM is reserved across jobs and hard-limited per job process tree. Exceeding
  the limit may produce an OOM failure. Job swap is disabled. Memory locality
  is not managed, and unmanaged applications can still create host pressure.
- Exclusive means no other managed job runs concurrently. Desktop processes,
  unrelated agents, interrupts and unmanaged workloads remain. This does not
  promise pristine benchmark timing. The default memory budget leaves headroom.
- GPU support is zero or one configured GPU. Ownership and CUDA_VISIBLE_DEVICES
  are cooperative; device access and VRAM are not enforced. Multi-GPU allocation
  and CPU/GPU locality are not implemented.
- Native child processes must stay in the job's cgroup. External Docker daemons,
  remote MPI ranks and deliberate cgroup escapes are outside the supported scope.
- Source code, inputs and output directories are not snapshotted or locked.
  Use separate experiment directories and do not edit queued/running jobs' inputs.
- Disk log retention is manual. Keep receipts private and preserve needed history.
- Scheduler/interface tests run without host privileges. Kernel enforcement,
  real daemon recovery and GPU execution require separate host validation.

## operations

After installing the 0.4 daemon at a planned maintenance window, an operator
can cancel a job without its control token:

    sudo /absolute/path/to/rmgr --home /home/srn/.local/state/rmgr \
      admin-cancel JOB_ID --reason 'Stopped by the operator'

Use a trusted installed executable and an explicit --home. UID 0 is checked from
SO_PEERCRED, not request arguments or environment. This does not configure sudo
or authorize agents to use sudo. Ordinary cancellation still requires the key.
The daemon transactionally records the audit reason, timestamp, peer UID, and
previous state before stopping a unit; inspect exposes operator_actions.
Queued work becomes cancelled, active work becomes cancelling, and reservations
remain until termination is observed. Already-terminal outcomes are preserved.
Legacy jobs without tokens are supported. A running new daemon is required;
there is no direct-database or systemctl fallback. The read-only preview refuses
all cancellation, including admin-cancel.

Ordinary agents should use health/resources and job commands. An operator
installs the package, initializes configuration, runs doctor, and installs the
service. Do not repair a shared service during another agent's experiment.

    pipx install /path/to/checkout
    rmgr init
    rmgr doctor
    rmgr service install
    rmgr health --json

The default daemon state directory is $XDG_STATE_HOME/rmgr, or
~/.local/state/rmgr. It must be owned by the user with mode 0700. The socket
checks the caller's Linux UID. All agents must point at the same state directory.
A different --home creates a separate resource pool, not an isolated share of
the same hardware; do not use that to work around pending jobs.
Service actions manage one fixed rmgr.service. Only render/install embed the
chosen --home in that service; start/stop/restart/status do not select another
unit based on --home. Drain jobs before changing configured resource capacity;
queued requests are not resized and may stop fitting after a reduction.

`health` checks the API, SQLite and systemd connection; it reports ready or
degraded. `resources` reports CPU/RAM/GPU reservations. `doctor` additionally
starts a short enforcement probe, so it refuses while the daemon or managed
jobs are running. A socket file's existence does not establish daemon readiness.

GPU fields under enforcement in health and a successful doctor:

- gpu_device_isolation=false means GPU device access is not enforced; it does
  not mean the GPU is inaccessible. This replaces the old gpu_device_access name.
- gpu_reservation reflects whether gpu_count=1 enables one managed GPU job at
  a time. With gpu_count=0 it is false; set the count to 1 once GPU 0 is ready.
- gpu_availability="not_checked" means hardware and drivers have not been tested.
- gpu_note explains the current configuration and cooperative access limits.

CUDA_VISIBLE_DEVICES controls visibility for cooperating programs. Programs
outside Runwarden can still access the GPU. After upgrading the installed
package, restart the daemon to update its health output.

Common errors:

- DAEMON_UNAVAILABLE: check the chosen --home and service status. A sandbox can
  block access even if the host daemon is healthy. Do not launch outside rmgr.
- BACKEND_UNAVAILABLE / ENFORCEMENT_UNAVAILABLE: ask the operator to inspect
  systemd/cgroup setup. There is no fallback to unenforced execution.
- CONTROL_TOKEN_REQUIRED / FORBIDDEN: use the correct private submission receipt;
  do not cancel via another route or attempt to retrieve somebody else's token.
- RECEIPT_EXISTS: use retry with that receipt; new experiments need a new path.
- WRONG_DAEMON: supply the receipt's original --home.
- REQUEST_CONFLICT: the request ID belongs to a different specification.
- LEGACY_JOB: the job predates tokens; operator handling is required.
- LOG_UNAVAILABLE: a recorded output was replaced, is not a regular file, or
  could not be read. Inspect the recorded path and preserve the original output.
- INVALID_REQUEST / NOT_FOUND: inspect arguments, resource capacity and job IDs.
- BUSY: doctor cannot probe without potentially disturbing managed work.

Jobs run in independent systemd units. Scheduler disconnection/restart does not
cancel them. Recovery reconciles reservations before admitting new jobs; backend
uncertainty retains reservations. Missing units become interrupted, never
automatically rerun. A machine reboot interrupts running jobs; queued jobs remain.

For operator diagnosis: `rmgr service status`, `rmgr service render`, and
`journalctl --user -u rmgr.service -n 100`. Changes to system cgroup delegation
and user lingering are separate, explicit host setup steps. rmgr never uses
sudo automatically. Stop the scheduler before package upgrades and preserve
its state and private receipts. Live validation instructions are in the project
README/docs; runtime capabilities must be checked on the actual host.

Host setup for a missing cpuset or memory controller:

    systemctl show "user@$(id -u).service" -p Delegate -p DelegateControllers

The cpu and cpuset controllers are separate. A user manager with only
`cpu memory pids` delegated cannot enforce CPU core assignments; its jobs lack
`cpuset.cpus.effective`. An administrator can add an account-specific override:

    sudo systemctl edit "user@$(id -u).service"

Enter these two lines in the override:

    [Service]
    Delegate=cpu cpuset memory pids

Apply at a planned reboot or full logout/login that restarts the user manager,
then rerun `rmgr doctor`. Lingering or other open sessions may keep the manager
alive after logout. Restarting it during work can terminate the desktop and
user services. A missing `memory.swap.max` instead requires checking kernel
cgroup v2 swap accounting support. The tool never applies these changes itself.
