# Desktop Agent ingress, state, and dormant application actions

The backend accepts an authenticated outbound connection from the standalone
Windows Desktop Agent, observes agent/session state, and registers three narrow
application capabilities: `desktop.app.list`, `desktop.app.status`, and
`desktop.app.launch`. They are dormant under the committed default-off ingress
gates. There are no Admin controls and no conversational planner exposure.

## Trust boundary and enforcement

| Property | Enforcement layer |
| --- | --- |
| TLS server identity before token disclosure | The standalone agent's `Client.session` verifies the configured SHA-256 SPKI pin before sending the hello/token frame. There is no trust-store fallback. |
| Independent machine token | `AgentHub._authenticate_hello` compares SHA-256 of the 64-character agent token with the root-managed configured hash using constant-time comparison. Browser sessions, cookies, passkeys, and Tailscale identity are not consulted. |
| Signed protocol frames | The shared standalone `butters_agent.protocol.verify` verifies HMAC-SHA256 over canonical JSON using the separately provisioned 32-byte command key. |
| Connection binding | `AgentHub` mints a random 256-bit connection ID; shared protocol verification requires it on every signed heartbeat, acknowledgment, result, and error. A later valid connection supersedes and closes the prior socket, and deterministically fails work owned by the old connection. |
| Timestamp freshness | Shared protocol verification admits signed frames only within the protocol-v1 plus/minus 90-second issuance window. |
| Replay protection | `AgentHub.socket` requires strictly increasing heartbeat sequence numbers and rejects duplicate terminal responses. The agent's bounded `ReplayCache` binds request IDs and idempotency keys to an action/target/parameter fingerprint; conflicting reuse fails closed. An error without a valid request ID is never assigned to the only pending request. |
| Browser `Origin` refusal | `AgentHub.socket` rejects any Origin-bearing socket before acceptance. The TLS ingress handshake allowlist also rejects `Origin`. |
| Browser identity stripping | `agent_ingress.validate_handshake` accepts only Host and the four required WebSocket upgrade headers. Cookie, Authorization, Tailscale identity, forwarded identity, CSRF, and all other headers are rejected and never reach Butters. |
| Private-only listener | `agent_ingress.bind_addresses` resolves only explicitly configured interfaces or a host and refuses wildcard, unspecified, public, or non-IPv4 addresses. Only `/agent/v1/session` is forwarded to the loopback web daemon. |
| Secrets at rest | The committed configs contain placeholders only. `/etc/butters/desktop-agent.toml` stores a token hash and a command-key file reference; the key and TLS private key remain separate root-managed files. Both key files must be regular, owned by root or the running service identity, and no more permissive than group-readable (`0640` is supported). No secret is placed in a systemd unit. |

## Truthful state

`AgentHub.snapshot()` returns the architecture's independent facets:

- `desktop.agent`: `not_configured`, `disconnected`, `awaiting_heartbeat`,
  `heartbeat_stale`, `heartbeat_aging`, or `connected`.
- `desktop.interactive_session`: `present`, `absent`, or `unknown`.

An authenticated hello is not called connected until a valid signed heartbeat
arrives. Disconnect discards the session observation. Network reachability,
SSH, power, and Parsec state never substitute for either agent facet.

The agent sends a heartbeat every 15 seconds. A heartbeat is fresh below 30
seconds old, aging from 30 to below 45 seconds, and stale from 45 seconds until
the socket idle timeout disconnects it at 60 seconds. Configuration validation
requires the strict ordering `aging < stale < idle`.

The existing read-only Admin overview response includes this safe snapshot for
operator observation. It contains no connection identifier, credential,
reported action list, or invocation mechanism, and no Admin control is added.

## Application capability boundary

The registered backend skills execute through `SkillRegistry`,
`PolicyValidator`, and, for launch, `ActionCoordinator`. `AgentHub` exposes only
`list_apps()`, `app_status(name)`, and `launch_app(name)`; it has no public
generic command/invoke/execute API.

- List and status are administrator-audience, read-only observations.
- Launch is an administrator-audience `ACTION`, requires explicit intent and
  existing elevated authentication, and does not require destructive/FRESH
  authentication or a separate confirmation.
- All schemas reject extra properties. Status and launch accept only one
  symbolic `app` matching `^[a-z][a-z0-9_]{0,63}$`.
- The agent-owned `apps.toml` is the only executable mapping. Butters first
  obtains the symbolic catalog from the active agent connection and never
  accepts or returns executable paths, argv, commands, shell/PowerShell,
  working directories, environment, hosts, addresses, users, or credentials.
- List does not require an interactive session. Status can truthfully inspect
  the agent's own registered-session processes even when the desktop is locked.
  Launch requires the fresh `desktop.interactive_session=present` facet; the
  agent additionally requires its narrower `gui_launch` observation, so a
  locked or ambiguous session still fails closed.

Requests use UUIDv4 request and idempotency identities, a configured deadline
(30 seconds by default), a bounded acknowledgment phase, and signed terminal
results. The launch skill hashes its stable `ActionCoordinator` job identity,
then explicitly sets the RFC-4122 version-4 and variant bits; the same job
therefore derives the same protocol-valid key, while a missing job identity
fails closed. Timeout, disconnect, supersession, malformed/replayed response,
and structured agent errors all terminate the request. Receipt/acknowledgment
is not reported as launch success: success requires the agent's terminal
`running` or `already_running` observation. Launching an already-running app is
successful and does not kill, restart, or duplicate the process. The agent can
return its cached terminal result when it receives the same key again, although
the current `ActionCoordinator` does not automatically redeliver launch
requests. Reuse for different work fails closed. The agent replay cache is
in-memory only and does not survive an agent process restart.

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

## Deployment status

These capabilities have not been validated over the request/result path on the
Windows host. No port is bound or service enabled by this slice. Production
remains on historical commit `5c66593ee84212e1757e1a2bdd948c38a43ffa6a`
until Admin parity is restored in a later slice. Isolated hardware staging must
use separate credentials, configuration, state, and a non-production private
ingress endpoint.
