# Development

- [Run the source client](#run-the-source-client)
- [Tests](#tests)
- [Implementation and permissions](#implementation-and-permissions)

## Run the source client

From this checkout, use its Python module instead of the installed executable:

```bash
cd /path/to/runwarden
python3 -m rmgr.cli status
python3 -m rmgr.cli status --watch 2
python3 -m rmgr.cli list --text --finished --all
python3 -m rmgr.cli inspect JOB_ID --text
```

Use Python 3.11 or newer. These commands read the existing daemon and do not
install the package, restart the service, or submit/cancel jobs. For a nondefault
state directory, put `--home PATH` before the command. Tests requiring no live
daemon are available with `python3 -m unittest tests.test_views -v`.

To use the development client from any worktree **without changing its cwd**:

```bash
rmgr-dev() { /path/to/runwarden/scripts/rmgr-dev "$@"; }
rmgr-dev describe submit --json
rmgr-dev list --text --active --wide
rmgr-dev inspect JOB_ID --cwd-only
```

The launcher preserves the caller's cwd, so default execution/output directories
remain in the worktree where you invoke it. It does not install anything or start
a daemon. Like the installed CLI, actual submit/cancel commands still change jobs;
use only read commands against a shared service when evaluating the client.

For new daemon-side features without upgrading production, start the **synthetic,
read-only preview** from the checkout in one terminal:

```bash
python3 -m rmgr.preview
```

It prints commands containing its temporary `--home`. Run those in another
terminal from this checkout. You can also use that home for `validate`,
`list --all --text --wide`, and `inspect`. The preview has three synthetic jobs,
sample output files, and private sample receipts. It never calls systemd or
executes workloads, rejects submissions/cancellations, and removes temporary
state on Ctrl+C. Do not use its resource numbers as host measurements.

## Tests

```bash
python3 -m unittest discover -s tests -v
RMGR_SYSTEMD_TESTS=1 python3 -m unittest tests.test_systemd_integration -v
```

The opt-in suite needs a functional user systemd manager and delegated cgroups.
It uses short jobs, no more than two physical cores, and a 512 MiB aggregate
budget with private temporary state. It does not install services or change host
configuration. Run it only when its resource use will not interfere with work.

For interface checks without real operator cancellation:

```bash
python3 -m unittest tests.test_agent_tools -v
```

Those tests simulate peer authorization and backend stop requests. Actual
privileged cancellation was checked separately inside a disposable VM; see the
[dated validation record](validation.md#installation-and-recovery-checks-on-2026-11-14).
A passing mocked test alone does not establish kernel enforcement.

## Implementation and permissions

The source separates request validation, persistence, scheduling, systemd
execution, transport, and CLI concerns. It uses standard-library `unittest` and
test doubles for scheduler failures, plus opt-in cgroup tests.

See [development permissions](development-permissions.md) before granting agents
host access. Access to the rmgr socket permits execution as the daemon's user,
outside a submitting client's sandbox. There is no cluster or container backend.
For workload examples, see [demo jobs](demo-jobs.md); for historical findings,
see the [0.3 code/documentation audit](code-documentation-audit.md).
