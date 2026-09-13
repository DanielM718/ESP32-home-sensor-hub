# Butters Windows Desktop Agent

This directory contains the standalone, outbound-only Windows agent for
interactive desktop and application observation and control.

## Reintegration status

Butters now registers `desktop.app.list`, `desktop.app.status`, and
`desktop.app.launch` over this package's signed protocol. The server-side hub
has only capability-specific application methods; no generic command or invoke
surface is exposed. Butters receives symbolic names and safe status metadata,
while this package remains the sole owner of executable mappings.

There is no planner or Admin control exposure. Both ingress gates remain
default-disabled, the request/result path has not been validated on Windows
hardware, and this code has not been deployed. Production remains on the
historical richer deployment until a later Admin-parity slice.

## Security model

The agent initiates a WSS connection; it never opens a Windows listener. It
pins the server's SHA-256 SPKI identity before sending its token, and all
post-handshake frames are HMAC-SHA256 signed over canonical JSON. Signed frames
are bound to a server-minted connection identifier and accepted only inside a
90-second issuance window.

The wire protocol is intentionally closed. Callers can select only allowlisted
symbolic actions and symbolic application names matching
`^[a-z][a-z0-9_]{0,63}$`. Arbitrary executable paths, command lines, argument
vectors, shell fragments, and caller-supplied PowerShell are unsupported.
Executable paths exist only in the operator-controlled local `apps.toml`.

Terminal results are cached in memory by request ID and idempotency key. The
authorized coordinator launch caller derives a deterministic UUIDv4-shaped key
from its stable job identity and fails closed when that identity is missing. A
key reused for different work fails closed. The replay cache is neither durable
nor a command queue and does not survive an agent process restart. Application
launch also observes already-running processes to avoid duplicate launches.

Rejected request frames carry their validated UUID request ID when available.
Connection-level errors without a valid identity remain unbound and must not be
attributed to a pending request by a future ingress merely because it is the
only pending request. This is an additive protocol-v1 compatibility tightening:
existing receivers already tolerate the optional field.

## Application registry

Copy `apps.example.toml` to `apps.toml` on the Windows host and add only paths
discovered on that host. Each key is the symbolic name used over the protocol:

```toml
schema_version = 1

[apps.git_bash]
path = 'C:\Program Files\Git\git-bash.exe'
images = ['C:\Program Files\Git\usr\bin\mintty.exe']
require_visible = true
```

The Windows platform validates that the registry and its parent directory are
owned by SYSTEM or Administrators and are not writable by other principals.
Paths must be absolute `.exe` paths. Launch uses `shell=False`, and the child
process is checked against the agent's interactive session.

## Local package checks

Repository pytest configuration adds `butters-agent/src` to the test import
path, so an editable install is not required:

```sh
python -m pytest butters-agent/tests
PYTHONPATH=butters-agent/src python -c \
  "import butters_agent.protocol, butters_agent.engine, butters_agent.client"
```

Tests run cross-platform with `platform.fake`; they do not require Windows.
The Windows boundary is tested by inspecting command construction and encoded
PowerShell input, while real session and ACL behavior still requires later
hardware validation.

## Windows-local helpers

`python -m butters_agent.provision` reads the token and command key from
protected standard input and stores them using user-scoped DPAPI. It is
create-only and does not deploy the agent or configure Butters.

`python -m butters_agent.selftest` exercises only locally registered
applications in the current interactive Windows session and records that
physical visual confirmation has not been performed. Installation and task
registration are intentionally deferred to a later deployment slice.
