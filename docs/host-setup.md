# Host setup and operations

Use the [README quickstart](../README.md#install-and-verify) for a first installation.
This guide covers failures and operator tasks. The installed package also provides
`rmgr manual host-setup` offline.

- [Missing CPU or memory delegation](#missing-cpu-or-memory-delegation)
- [Other startup failures](#other-startup-failures)
- [State and service](#state-and-service)
- [Startup at boot and after logout](#startup-at-boot-and-after-logout)
- [Upgrade or repair](#upgrade-or-repair)
- [Compatibility with older installations](#compatibility-with-older-installations)
- [Operator cancellation without a job key](#operator-cancellation-without-a-job-key)
- [Remove the service](#remove-the-service)

## Missing CPU or memory delegation

Requires Linux with cgroup v2, systemd 254+, and Python 3.11+. Run checks from a
normal host terminal. A bus connection failure can mean a restrictive sandbox or
missing user session; first check `systemctl --user status`.

```bash
systemctl show "user@$(id -u).service" -p Delegate -p DelegateControllers
```

The `cpu` and `cpuset` controllers are separate. Delegating `cpu memory pids`
without `cpuset` prevents enforced core assignments; the probe may report a
missing `cpuset.cpus.effective` file. If `cpuset` or `memory` is missing, an
administrator can create an account-specific override:

```bash
sudo systemctl edit "user@$(id -u).service"
```

Add this above the editor's discard line and save:

```ini
[Service]
Delegate=cpu cpuset memory pids
```

**Before rebooting an existing installation**, pause agent submissions, finish
or cancel all queued/running jobs, then run `rmgr service stop`. Stopping the
scheduler alone does not stop job units. Save other work before rebooting:

```bash
sudo reboot
```

Restarting only `rmgr.service` does not apply the delegation change. After login,
stop an existing rmgr service again before running `doctor`: it may have started
at boot. With no daemon or managed jobs active, rerun `rmgr doctor`, then
`rmgr service install && rmgr health --wait 30 --json` only if the probe passes.
For a first installation, return to the README's installation commands.

## Other startup failures

| Symptom | Check / action |
| --- | --- |
| `rmgr: command not found` after pipx installation | Run `pipx ensurepath`, then `export PATH="${PIPX_BIN_DIR:-$HOME/.local/bin}:$PATH"` in the current shell. |
| Missing packaging tools on Ubuntu 24.04 | Install `pipx` and `python3-venv` with the system package manager. |
| Cannot connect to the user bus | Run `systemctl --user status` in a normal login session, outside the agent sandbox. |
| `doctor` reports `BUSY` | Use `health` during operation. Drain jobs and stop the service before probing enforcement. |
| Missing `memory.swap.max` | Check kernel cgroup v2 swap-accounting support; changing CPU delegation cannot restore that interface. |
| Health never becomes ready | Inspect service state and the journal below. Do not assume a socket file proves readiness. |

```bash
rmgr service status
journalctl --user -u rmgr.service -b -n 50 --no-pager
```

`init` creates configuration only if absent. `doctor` launches a short, one-core,
64 MiB probe to verify actual CPU and memory settings. It does not repair the host
or fall back to unenforced execution. `health` checks the API, database, and
systemd connection; `health --wait 30` waits for readiness and returns nonzero on
failure. Neither command tests GPU hardware or drivers.

## State and service

The state directory is `$XDG_STATE_HOME/rmgr`, or `~/.local/state/rmgr`, and must
be private (mode 0700). It contains `config.json`, `jobs.sqlite3`, the control
socket, and launch records. Program output is stored separately in job working
directories. See [output files](usage.md#output-files-and-manager-records).

Pass `--home /absolute/path` before the command for a nondefault state directory.
Use one shared daemon/state directory for agents on the same machine. Service
commands always manage the single `rmgr.service`: `--home` changes the path
embedded by `service render` or `service install`, not the unit selected by
`start`, `stop`, or `status`.

The installer writes the user service, reloads the user manager, and enables and
starts that service without sudo. Inspect it first with `rmgr service render`.
Jobs run in independent units; scheduler restarts preserve running jobs, but
reboot interrupts them. Queued jobs remain durable. See [recovery](usage.md#recovery-states-and-exit-codes).

## Startup at boot and after logout

```bash
loginctl enable-linger "$USER"
```

This may require administrator authorization. It is optional for jobs run while
logged in. Lingering or other sessions can keep the user manager alive after
logout, so logging out alone may not apply a delegation change.

## Upgrade or repair

Pause submissions, finish/cancel managed jobs, stop the service, and back up the
state directory and private receipts before replacing package files. Do not
upgrade while a launcher is starting. From the checkout, with pipx's executable
directory on PATH:

```bash
rmgr service stop &&
pipx install --force . &&
rmgr init &&
rmgr doctor &&
rmgr service install &&
rmgr health --wait 30 --json
```

For a virtualenv upgrade, use `.venv/bin/rmgr` and replace the pipx command with
`.venv/bin/python -m pip install --upgrade .`. Keep the environment at its original
path. Starting the service after replacing the package ensures the daemon uses
the updated code.

For host repair with this version already installed, skip only the package
installation step. Run `init`, `doctor`, `service install`, and `health --wait 30`
with the service initially stopped and no managed jobs active. An older client
must be upgraded to get `manual host-setup` and `health --wait`; changing delegation
alone does not add commands.

Changing resource capacity is separate: drain work before reducing capacity,
because queued jobs retain their original requests and may stop fitting.

## Compatibility with older installations

Check `rmgr --version` for the client and `rmgr health --json` for the running
daemon. A newer client does not upgrade the daemon. Missing `features` means
legacy/unknown; do not infer API support from client version alone.

| Version | Upgrade considerations |
| --- | --- |
| 0.2 | Adds control-token hashes while preserving history. Finish/cancel 0.1 jobs before upgrading: they have no token for ordinary cancellation. Their history remains readable. |
| 0.3 | Adds output paths in working directories. Existing jobs retain their old central paths; no files are moved and old receipts remain valid. Upgrade and restart the daemon for the new behavior. |
| 0.4 development | Adds validation, snapshots, log tail/follow, structured queue details, and operator cancellation. The client keeps tokens out of normal submit/retry output unless `--show-token` is requested. Check daemon capabilities before using new features. |

Basic text views can query older daemons; immutable snapshots and log tail
need a 0.4 daemon. Following without tail uses the ordinary log API. `lookup` falls back to searching public history on older daemons.
Old cached GPU enforcement reports are translated on startup; this does not
establish hardware availability. See [GPU fields](usage.md#gpu-health-fields).

## Operator cancellation without a job key

A human operator can use a trusted installed executable with an explicit target
state directory and a running 0.4 daemon:

```bash
sudo /absolute/path/to/rmgr --home /absolute/path/to/user-state/rmgr \
  admin-cancel JOB_ID --reason 'Stopped by the operator'
```

This is not an agent workaround for denied cancellation. The daemon verifies
UID 0 from Unix-socket peer credentials; request fields cannot grant authority.
Runwarden does not modify sudoers or invoke sudo automatically.

An audit entry and cancellation intent are committed before termination. Queued
jobs become cancelled; running jobs enter `cancelling` and retain reservations
until their processes stop. Completed outcomes are preserved. `inspect` exposes
`operator_actions` with the time, UID, reason, and previous state. Legacy jobs
without tokens can also be cancelled this way.

A daemon connection is required; the command does not edit SQLite or kill
processes behind a stopped daemon. Ordinary agents still need the matching receipt.
See [validation](validation.md#installation-and-recovery-checks-on-2026-11-14)
for the disposable-VM operator check and its scope.

## Remove the service

First finish/cancel outstanding jobs, then disable the service:

```bash
systemctl --user disable --now rmgr.service
```

Remove the generated `rmgr.service` file from the user systemd directory and run
`systemctl --user daemon-reload`. Removing the service or package does not delete
job history or program output.
