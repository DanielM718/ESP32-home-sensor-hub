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
