# Butters NAS Agent

This is a standalone, outbound-only agent for the one configured NAS identity,
`nas-primary`. It implements four closed schemas: three read-only status calls
and a fixed shutdown operation that ships disabled. It has no shell, command,
argv, caller-selected URL, middleware method, or generic proxy surface.

The Butters machine token/HMAC key, TrueNAS read key, and dormant TrueNAS
shutdown key are separate files. The FULL_ADMIN shutdown key is not loaded at
all while `shutdown_enabled = false`.

See `docs/NAS_AGENT_DESIGN.md` and `docs/NAS_AGENT_STAGING.md` for trust,
packaging, acceptance, and rollback details. No example contains a credential.

Create a new machine identity with:

```console
butters-nas-agent-provision --output-dir /absolute/new/private/directory
```

The command is create-only. It writes the agent credential, the independent
Butters credential configuration, and the HMAC key as mode `0600` files under
a new mode `0700` directory. Standard output contains paths and the token
digest, but never the token or signing key.
