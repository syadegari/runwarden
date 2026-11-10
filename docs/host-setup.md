# Host setup

The application installer needs no root access. CPU/memory cgroup delegation
and user-service persistence may need a separate, one-time administrator step.
The manager never edits host configuration or changes drivers automatically.

1. Install the package in a stable pipx/virtualenv environment.
2. Run `rmgr init`, then `rmgr doctor` from a normal host terminal.
3. If the doctor succeeds, install the service and check `rmgr health --json`.

`Failed to connect to bus` can mean the command is inside a restrictive sandbox
or has no user systemd session. It does not establish that cgroup delegation is
missing. First check `systemctl --user status` from your ordinary terminal.

If the bus works but CPU or memory restrictions cannot be applied, an
administrator can inspect your account's user-manager unit:

```bash
systemctl show "user@$(id -u).service" -p Delegate -p DelegateControllers
```

If required, create a drop-in for that specific account, using
`sudo systemctl edit "user@$(id -u).service"`:

```ini
[Service]
Delegate=cpu cpuset memory pids
```

Apply it at a planned logout/login or reboot. Restarting an active user manager
can terminate that user's services, so do not do that in the middle of work.
Rerun `rmgr doctor` afterward; the actual execution probe is the deciding check.
See [systemd resource controls](https://freedesktop.org/software/systemd/man/latest/systemd.resource-control.html).

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

For upgrades, stop the scheduler, upgrade its package, then start it and check
health. Independent job units remain alive. Back up state before schema-changing
releases. Version 0.2 adds control-token hashes without replacing history;
finish/cancel 0.1 jobs before upgrading because those jobs have no token and
cannot be cancelled through the new API. Private client receipts must also be
preserved. Do not upgrade package files while a job launcher is starting.
