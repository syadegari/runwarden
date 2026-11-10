# Development permissions

Prefer a project-scoped sandbox with explicit capabilities over unrestricted
host access. Routine code changes, tests, packaging and local commits should
not require repeated prompts once their paths are authorized.

Recommended boundaries:

| Capability | Scope |
| --- | --- |
| Write files | This checkout, its `.git`, disposable test/checkouts, a project virtualenv |
| Read environment | Installed tools and the existing `py3.14` environment |
| Network downloads | GitHub and Python package hosts through an enforced domain filter |
| Git | Local branches, add, diff, commit; publishing/force-push remain separate decisions |
| User installation | Only explicitly selected rmgr state, package, and service paths |
| Host configuration | One-time reviewed cgroup/linger setup; no blanket sudo |
| GPU | Normal device access for bounded tests, not driver replacement or reset |

Current Codex permission profiles can extend the workspace preset and explicitly
make `.git` writable. Domain rules require an active network proxy. For zero
approval prompts use `approval_policy = "never"`; prohibited actions will fail.
Alternatively, `on-request` with `approvals_reviewer = "auto_review"` delegates
eligible approval decisions. See [official permissions](https://learn.chatgpt.com/docs/permissions)
and [approval policies](https://learn.chatgpt.com/docs/agent-approvals-security).

An example for Codex versions supporting these profiles:

```toml
approval_policy = "never"
default_permissions = "rmgr-development"

[features]
network_proxy = true

[permissions.rmgr-development]
extends = ":workspace"

[permissions.rmgr-development.filesystem.":workspace_roots"]
".git" = "write"

[permissions.rmgr-development.network]
enabled = true

[permissions.rmgr-development.network.domains]
"github.com" = "allow"
"api.github.com" = "allow"
"raw.githubusercontent.com" = "allow"
"codeload.github.com" = "allow"
"pypi.org" = "allow"
"files.pythonhosted.org" = "allow"
```

This leaves the workspace preset's other protections in place. Configure it in
the Codex surface that launches the session; a chat instruction cannot override
an enforced permission profile. An organization-managed policy may restrict
which changes are available. Git LFS or other package registries may require
additional exact hosts when actually used. A network allowlist limits destinations,
not whether a command clones or pushes; Git credentials and explicit publishing
instructions remain relevant. Writable Git metadata can also change hooks/config.

Host integration deserves a separate boundary. Access to the user systemd bus
or rmgr submission socket permits running arbitrary programs as that account,
outside a submitting agent's filesystem sandbox. Allowing those sockets broadly
is not equivalent to granting only project writes. Prefer running the small
reviewable host-validation suite yourself, or use a dedicated Unix test account
whose writable data is disposable. The same applies to a rootful Docker socket,
which grants much more authority than a normal project directory.

The current session allows project and temporary-directory edits but protects
`.git`, denies Unix-socket binding/user-systemd access, and blocks direct GitHub
cloning. It cannot install the user service or alter those restrictions itself.
