# Code/documentation consistency audit — 2026-11-11

Scope: all tracked runtime modules, CLI/manual, packaging metadata, tests,
examples, README and documents under `docs/`. The review compared documented
commands, defaults, ownership, persistence, scheduling and lifecycle behavior
against the implementation. External projects and tool-provider configuration
schemas were not independently re-audited; historical FE/GPU measurements remain
identified by the revision and environment in [validation](validation.md).

## Findings and corrections

| Area | Finding | Resolution |
| --- | --- | --- |
| Program output | stdout/stderr were central files; guidance conflated these with manager records. | New 0.3 jobs use unique files in job cwd or explicit `--stdout`/`--stderr` paths. SQLite records locations. README, bundled manual, demo commands and host upgrade notes describe the same behavior. |
| Launcher failures | Runner diagnostics were mixed into program stderr; failures opening logs could escape the launch-error handling. | File opening is covered by launcher error handling. Diagnostics stay in the central launch record, job reason and journal. Collisions fail the affected job without overwriting files or stopping the queue. |
| File identity and retention | A saved path alone could cause a colliding or replaced file to be presented as another job's output. | New launches record device/inode metadata after both files are created. Log reads verify it and report missing files with `available=false`; replacement or unsafe/nonregular targets produce `LOG_UNAVAILABLE`. No automatic retention or relocation is promised. |
| Upgrade compatibility | New default paths could otherwise relocate historical/queued outputs or change receipt comparison. | Nullable database columns preserve old central paths; old manifests still work. Omitted optional fields preserve pre-0.3 canonical specs. Migration, retry and legacy launch behavior are tested. |
| Accepted retries | Documentation promised recovery of the original job, but retries revalidated its cwd and resources; admission degradation also rejected accepted retries. | An authenticated identical request returns its persisted job before current cwd/capacity/readiness checks. New requests still undergo validation; wrong keys and changed specs remain rejected. |
| Backend readiness | An idle scheduler could accept a new submission even after health reported that systemd was unreachable. | New API submissions check backend connectivity. Already-accepted retries remain available without it. Regression checks cover both cases. |
| Service selection | README implied `--home` selected state for every service action. Code always manages one `rmgr.service`. | Documentation now distinguishes render/install's embedded state path from lifecycle commands targeting the fixed service unit. |
| Capacity changes | Generic stop/edit/restart advice omitted that queued jobs keep old requests and may no longer fit. | README, manual and host setup recommend draining work before reducing capacity and explain that requests are not resized. |
| CLI output and shell behavior | Guidance did not clearly distinguish submission output, program stdout, shell pipes and JSON formatting. | Direct `submit ... -- PROGRAM ...` is the primary workflow. Documentation states that submit returns immediately, program output is captured, shell operators require an explicit shell, and data commands use indented/compact JSON. Doctor/health diagnostic reports can be on stdout with exit code 2. |
| Agent onboarding | README instructions were not directly available as a CLI command. | `rmgr instructions` prints the bundled instructions topic to stdout, with optional `--json`. README points to that single source; instructions distinguish public IDs from secret keys and explain persistent receipt handoffs. |
| JSON specs | Resource/cwd/environment/output CLI flags do not override `--spec`, but this was unstated. | README and manual explicitly require job settings in the JSON file when using `--spec`; submission receipt/request-ID options still apply. |
| Demo helper | Output viewing assumed central paths. | Helper supplies separate named stdout/stderr files in each fresh run directory and records their paths. Examples query `inspect` rather than guessing paths, including for legacy jobs. |
| Validation and permissions | Earlier sandbox limitations were phrased as current facts despite later successful approved host tests. | Historical results now use past tense; current evidence and permission limitations are separated. |
| Release metadata | The output layout and database schema change needed a distinguishable release. | Runtime and package versions are both 0.3.0, with a consistency test and upgrade instructions. |

## Validation

- The complete 60-test suite passed with host access and no skips, including
  seven live systemd tests and Unix socket transport. It used temporary state,
  at most two cores and a 512 MiB aggregate budget.
- After the readiness correction, the 39 interface/output/control tests passed.
  These include real subprocess output redirection, preservation of existing
  files, missing/replaced outputs, migration, authenticated retries and private
  token handling.
- The 0.3 wheel built offline with no runtime dependencies. A disposable installed
  wheel/daemon check exercised CLI submission, explicit stdout/stderr, retrieval,
  the packaged manual and a running job surviving a test-daemon restart.
- `git diff --check` and documentation command/option review were performed.
- After adding the offline instructions command, the 61-test suite passed in
  the sandbox with eight host-only checks skipped. The new check verifies both
  text/JSON forms without daemon access or creation of a state directory.

## Boundaries

The installed shared daemon and its configuration were not upgraded during this
work. Use the upgraded package and restart that daemon to obtain 0.3 behavior.
Old jobs continue using their original output locations.

Runwarden does not relocate or retain a second copy of working-directory outputs.
Moving/deleting them makes API retrieval unavailable. New output targets must
not exist and their parent directories must exist. A failed launch may leave
empty files created before the failure. Explicit paths in a shared run directory
must be unique; the generated default names already include the job ID.

The earlier 16-thread native FE comparison completed as a managed job but printed
`match=False`; its numerical comparison is not described as passing. Full
nonlinear FE execution, PETSc/MPI execution and an upstream SuperLU_MT race study
remain outside the evidence recorded here. GPU kernels were checked previously;
they were not rerun for the output-path change. API control keys continue to
prevent accidental cross-agent actions, not unrestricted same-account access.
