# NAS Agent staging and acceptance runbook

This runbook does not authorize deployment or shutdown. Execute it only in a
later approved staging window. Record every image/config digest, test result,
and rollback action.

## Preconditions

- Rebase or merge the reviewed branch onto current `main`; rerun the full suite.
- Confirm the TrueNAS appliance patch is in the 25.10 series and validate the
  matching `/api/current` documentation.
- Build the NAS Agent image from the reviewed commit, scan it, publish it, and
  replace the Compose placeholder with the immutable `sha256` digest.
- Prepare a staging Butters endpoint/certificate. Keep production ingress and
  both shutdown gates disabled.
- Generate a unique 32-byte NAS machine token and independent 32-byte HMAC key.
  Store only the token digest plus HMAC key on staging Butters, and the token
  plus HMAC key in the NAS-side secret file. Never reuse Desktop credentials.
- Create only the dedicated TrueNAS read-only service identity/key needed for
  the three reviewed status methods. Do not create the FULL_ADMIN shutdown key.
- Record the TrueNAS middleware certificate SPKI in non-secret agent
  configuration. Put secret files in a dedicated dataset/ACL. Verify files are
  regular, owner-readable
  only (`0600` for secrets), and visible only to UID/GID 568.

## Read-only acceptance

1. **TLS/SPKI:** start with a deliberately wrong pin. Confirm the agent sends no
   token/hello and reports `server_identity_mismatch`; then install the measured
   staging SPKI pin.
2. **Machine auth:** confirm `nas-primary` connects. Try a wrong token, Desktop
   token, wrong identity, missing action, and extra action; each must fail
   closed without altering the valid session.
3. **Heartbeat:** observe `awaiting_heartbeat -> connected`, then pause traffic
   and observe `heartbeat_aging -> heartbeat_stale -> disconnected` at the
   configured thresholds. No old TrueNAS/Jellyfin payload may survive a
   disconnect or reconnect.
4. **Read status:** invoke `nas.agent.status` and `nas.system.status`; compare
   only the bounded fields with local TrueNAS state. Confirm no key, raw JSON,
   network URL, or middleware body appears in responses or logs.
5. **Jellyfin:** test healthy, stopped/unreachable, and non-200 health responses.
   Confirm the agent stays healthy while Jellyfin is down and only the fixed
   configured endpoint is contacted.
6. **Reconnect/supersession:** restart only the staging agent and then overlap
   two authenticated instances. The new connection must close the old one;
   late old frames cannot change state or satisfy a request.
7. **Idempotency:** resend an identical request and idempotency key and confirm
   the cached result is marked duplicate. Reuse either key with a different
   fingerprint and confirm rejection. Exercise ACK/result timeout and late
   terminal-frame tombstones.
8. **Power truth:** stop only the agent while leaving the NAS reachable. Butters
   must report power `unknown`, never `off`. Only agent disconnected plus fresh
   LAN-unreachable and API-unreachable observations may render `OFF`.

## Mock shutdown acceptance

9. Keep the NAS Agent shutdown gate false and omit the shutdown secret. Confirm
   administrator and `nas_power` callers receive capability unavailable;
   `jellyfin_access` is denied before action preparation.
10. In a non-hardware test container, substitute the reviewed fake backend and
    enable only its test gate. Verify explicit confirmation, FRESH passkey
    binding, `{}` arguments, deterministic coordinator idempotency, accepted,
    refusal, timeout, duplicate, and disconnect-after-ACK cases. Attempts to
    supply a URL, method, mode, delay, shell, command, or argv must fail before
    transport.

## Separately authorized real shutdown

Do not proceed without explicit owner approval of both the destructive test and
the documented `FULL_ADMIN` residual risk. At that later time:

1. Create a dedicated, expiring/revocable TrueNAS user-linked API key whose
   account has `FULL_ADMIN`; store it only in the NAS secret dataset.
2. Add the shutdown secret mount and `--truenas-shutdown-key` argument to a
   reviewed staging-only Compose copy. Flip both independent gates only for the
   window.
3. Ensure workloads and storage are safe to stop. From the authorized portal,
   confirm the exact plan and complete a fresh passkey ceremony.
4. Record the signed accepted result (or indeterminate result), agent
   disconnect, LAN/API unreachability, and final OFF transition. Do not infer
   OFF from the disconnect.
5. Use the existing fixed Butters WOL action to recover. Validate the entire
   observed boot lifecycle through Jellyfin ready.

## Rollback and cleanup

- Disable the Butters NAS Agent ingress and shutdown action gate.
- Disable/remove the Custom App using the TrueNAS UI; do not delete the dataset
  until logs and evidence are retained.
- Revoke the NAS machine token/HMAC pair and all TrueNAS service API keys;
  remove the shutdown mount/argument if it was ever approved.
- Restore the prior reviewed Butters artifact/config and verify existing WOL,
  NAS observer, Jellyfin portal, WebAuthn, and Desktop Agent health.
- Confirm no inbound port, host network, Docker socket, elevated capability,
  orphan container, active portal grant, or secret remains.
- If shutdown acceptance was indeterminate, inspect TrueNAS locally; do not
  retry automatically with the same hardware state unknown.
