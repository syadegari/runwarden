# rmgr manual

## overview

rmgr schedules native jobs for cooperating agents on one Linux workstation.
It reserves physical cores and RAM, offers exclusive execution among managed
jobs, and optionally serializes access to one GPU. Use the same daemon/state
directory as the other agents. Do not start a second daemon to bypass a queue.

Start here:

    rmgr --version
    rmgr health --json
    rmgr resources --json
    rmgr submit --help
    rmgr manual jobs
    rmgr manual ownership
    rmgr manual limits

This manual works offline without a daemon. Topics are overview, jobs,
ownership, limits, and operations. Use `rmgr manual TOPIC` for a short section,
`rmgr manual all` for the complete guide, or add `--json` for a text envelope
with version and topic information. No pager or interactive prompts are used.

All agents should read ownership and limits before launching experiments.
`--help` describes exact command syntax; this manual explains workflow and
semantics. Runtime readiness and capacity come from health/resources, not the
manual. The tool is experimental until the live host tests pass on your machine.

Pass a nondefault state directory before the command:

    rmgr --home /shared/private/rmgr-state health --json

CLI success output goes to stdout. Operational errors are JSON on stderr;
argument-parser usage errors are ordinary text. `--json` produces compact JSON
for data commands. Do not parse human formatting or interpret an exit code of
zero from `submit` as evidence that the experiment has finished.

## jobs

The normal interface is a command prefix:

    rmgr submit MANAGER_OPTIONS -- PROGRAM PROGRAM_ARGUMENTS

No wrapper script or JSON file is required. Everything after `--` is passed as
the target program's arguments, including its own flags. The manager queues
and launches that program when resources become available. `program | rmgr`
is not supported: the shell would start the program before admission and pipe
its output instead of submitting its command.

Choose a private receipt path unique to this submission and an experiment
directory that other jobs will not modify:

    rmgr submit --receipt ./experiment-01.receipt.json --cores 8 --memory 16G \
      --timeout 600 --exclusive -- /path/to/python /path/to/benchmark.py --workers 8

The CLI creates the receipt with mode 0600 before sending the request. The
submission response contains the public job `id`, a private `control_token`,
and the receipt path. Keep the receipt outside tracked/shared job artifacts.
Receipt paths are never returned by job queries. Existing receipts are never
overwritten: use retry to repeat a submission, or a new path for a new run.

    rmgr inspect JOB_ID --json
    rmgr logs JOB_ID --stream stdout --offset 0 --limit 16384 --json
    rmgr logs JOB_ID --stream stderr --json
    rmgr wait JOB_ID --timeout 30 --json
    rmgr cancel JOB_ID --receipt ./experiment-01.receipt.json

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
      "env": {"OMP_NUM_THREADS": "8"}
    }

argv and cwd are required. JSON executable/cwd paths must be absolute. CLI
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
until it is null. The maximum page size is 100. `--state` filters job state.

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
health, or runner manifests. The CLI adds the token only to submit/retry output.

Cancellation requires the matching token, even for an already completed job.
Use `cancel JOB_ID --receipt PATH`. Alternatively, `--token-stdin` reads a raw
token from stdin, keeping it out of process command-line arguments. A receipt
for another job will not authorize cancellation. There is no tokenless --force
option, job editing operation, or API for recovering a lost token.

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
history. Old jobs have no token and cannot be cancelled through the new API.
Finish/cancel old jobs with 0.1 before upgrading. Legacy history remains readable;
there is no automatic adoption or creation of credentials for somebody else's job.

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
