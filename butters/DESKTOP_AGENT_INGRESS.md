# Desktop Agent ingress and state observation

This slice is observer-only. It accepts an authenticated outbound connection
from the standalone Windows Desktop Agent and observes agent/session state. It
does not register an agent skill, send a command, expose an Admin control, or
add an agent capability to the conversational planner catalog.

## Trust boundary and enforcement

| Property | Enforcement layer |
| --- | --- |
| TLS server identity before token disclosure | The standalone agent's `Client.session` verifies the configured SHA-256 SPKI pin before sending the hello/token frame. There is no trust-store fallback. |
| Independent machine token | `AgentHub._authenticate_hello` compares SHA-256 of the 64-character agent token with the root-managed configured hash using constant-time comparison. Browser sessions, cookies, passkeys, and Tailscale identity are not consulted. |
| Signed protocol frames | The shared standalone `butters_agent.protocol.verify` verifies HMAC-SHA256 over canonical JSON using the separately provisioned 32-byte command key. |
| Connection binding | `AgentHub` mints a random 256-bit connection ID; shared protocol verification requires it on every signed heartbeat. A later valid connection supersedes and closes the prior socket. |
| Timestamp freshness | Shared protocol verification admits signed frames only within the protocol-v1 plus/minus 90-second issuance window. |
| Replay protection | `AgentHub.socket` requires a strictly increasing integer heartbeat sequence for the current connection. Repeated or decreasing sequences close the connection. The standalone package's `ReplayCache` remains responsible for request/idempotency replay protection in a future effector slice; no request frames are accepted here. |
| Browser `Origin` refusal | `AgentHub.socket` rejects any Origin-bearing socket before acceptance. The TLS ingress handshake allowlist also rejects `Origin`. |
| Browser identity stripping | `agent_ingress.validate_handshake` accepts only Host and the four required WebSocket upgrade headers. Cookie, Authorization, Tailscale identity, forwarded identity, CSRF, and all other headers are rejected and never reach Butters. |
| Private-only listener | `agent_ingress.bind_addresses` resolves only explicitly configured interfaces or a host and refuses wildcard, unspecified, public, or non-IPv4 addresses. Only `/agent/v1/session` is forwarded to the loopback web daemon. |
| Secrets at rest | The committed configs contain placeholders only. `/etc/butters/desktop-agent.toml` stores a token hash and a command-key file reference; the key and TLS private key remain separate root-managed files. No secret is placed in a systemd unit. |

## Truthful state

`AgentHub.snapshot()` returns the architecture's independent facets:

- `desktop.agent`: `not_configured`, `disconnected`, `awaiting_heartbeat`,
  `heartbeat_stale`, `heartbeat_aging`, or `connected`.
- `desktop.interactive_session`: `present`, `absent`, or `unknown`.

An authenticated hello is not called connected until a valid signed heartbeat
arrives. Disconnect discards the session observation. Network reachability,
SSH, power, and Parsec state never substitute for either agent facet.

The existing read-only Admin overview response includes this safe snapshot for
operator observation. It contains no connection identifier, credential,
reported action list, or invocation mechanism, and no Admin control is added.

## Configuration and installation

There are two independent default-off gates:

1. `[agent_ingress].enabled = false` in `config/assistant.toml` controls whether
   the loopback web application exposes the machine route and loads machine
   credentials.
2. `enabled = false` in `/etc/butters/agent-ingress.toml` controls whether the
   private TLS listener binds.

`scripts/install-agent-ingress` installs the reviewed standalone package,
root-managed example configs, and the hardened unit. With no arguments it does
not enable or start the unit and does not generate credentials. Configure both
files and provision the referenced TLS/key material before explicitly enabling
the service.
