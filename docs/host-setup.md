# Host setup

Follow [Host setup and Install in the README](../README.md#host-setup).
The same repair steps ship with the package: `rmgr manual host-setup`.

`init` creates configuration; `doctor` verifies enforcement. Stop on a failed
probe and follow its host-setup hint. After a passing probe:

```bash
rmgr service install && rmgr health --wait 30 --json
```

The health command waits for daemon readiness without displaying transient
startup errors. On failure, inspect `rmgr service status` and
`journalctl --user -u rmgr.service -b -n 50 --no-pager`.

For startup at boot and persistence after logout:

```bash
loginctl enable-linger "$USER"
```

This may require administrator authorization. It is not required just to run
jobs while logged in. See [systemd user-service behavior](https://www.freedesktop.org/software/systemd/man/258/systemd-run.html).

Inspect the generated service before installing it with `rmgr service render`.
To remove the service later, first finish/cancel outstanding jobs, then disable
it with `systemctl --user disable --now rmgr.service`, remove its generated unit
file from the user systemd directory, and run `systemctl --user daemon-reload`.
Removing the package or service does not delete job history.

For upgrades, follow [Upgrade or repair](../README.md#upgrade-or-repair): pause
submissions, finish/cancel managed jobs, stop the scheduler, upgrade the package,
then run doctor and install/start the service. Stopping the scheduler alone leaves
independent job units alive. Back up state before schema-changing releases.
Version 0.2 adds control-token hashes without replacing history;
finish/cancel 0.1 jobs before upgrading because those jobs have no token and
cannot be cancelled through the new API. Private client receipts must also be
preserved. Do not upgrade package files while a job launcher is starting.

Version 0.3 adds output-path columns and writes new jobs' stdout/stderr in their
working directories. Existing history and queued/running jobs keep their central
output paths; old receipts still work. No output files are relocated. Upgrade
both the CLI and daemon package and restart the daemon before relying on the new
behavior. `inspect` reports the exact stdout_path/stderr_path for either layout.

Changing capacity is separate from upgrading: drain managed work before reducing
CPU/RAM/GPU capacity, because queued jobs keep their original requests. All
service lifecycle commands manage the same rmgr.service; --home affects its
rendered/installed configuration, not the unit selected by start/stop/status.
