# Desktop session management: completion of PRs #4–#6

Branch `feature/desktop-access-modes-completion-20261028` is stacked on draft
PR #6 (`fix/sunshine-display-identity-20261027`, `7aec7bf`), which is stacked on
PR #5 (`7682879`), which contains PR #4 (`5217a5f`, `560a725`) on `main`
`1cd55af`. It does not rewrite those commits. Merge order stays
#4 → #5 → #6 → this branch.

The work is source only: nothing here has run on Windows, Home Assistant, the
broker socket, or production. Every new capability ships disabled in
`butters/config/assistant.toml` and `butters/config/action-broker.example.toml`.

## Bugs found in the stack and fixed here

1. **The broker's SSH credential gate missed `desktop.sunshine_service_status`.**
   `broker_main` kept its own hand-written set of SSH operations, and PR #5's new
   operation was never added to it. With only that operation enabled, the broker
   started without checking the root-owned key or the pinned `known_hosts`. ssh
   itself still refused an unknown host (`StrictHostKeyChecking=yes`), so this
   let a misconfiguration through at startup rather than opening a hole. The set
   now lives in `broker.py` as `DESKTOP_SSH_OPERATIONS`, and a test checks it
   against every handler that actually runs ssh.
2. **A superseded request held the execution lock for its whole wait.** The
   wake wait (up to 120 s) and the SSH wait (up to 90 s) checked only
   cancellation and the deadline, so a newer Local request queued behind an
   older Gaming request until that window expired. `_wait_for` now takes a
   `stop` predicate, and every wait uses the generation check.
3. **A mutation could be sent with less time left than the broker needs.** The
   monitor call took whatever time was left. With under about 25 s left, the
   client gave up while the root broker was still changing the outlets, and a
   change that had happened was reported as `failed`. Every mutation now needs
   the broker's whole 30 s request timeout to remain, or it fails before
   sending anything (`desktop_mode_timeout`). If no broker result arrives, the
   response tells the operator to observe the outlets before retrying. The
   default `total_timeout_seconds` goes from 180 to 240 to cover the extra
   steps.
4. **`set_monitor_power` treated every value except `"off"` as "both on".** An
   unexpected value would have powered the intentionally-off OLED. It now uses
   an explicit table and raises on anything else. The legacy boolean fallback
   never stands in for the primary-only operation.
5. **`desktop-control.ps1` loaded `display-topology.ps1` for every operation.**
   That compiled its C# `QueryDisplayConfig` type in each new PowerShell process
   (Parsec, service status, shutdown), and a missing helper made all of those
   operations fail. It now loads only in `SunshineProbeInteractive`.
6. **Agent catalog overhead (PR #5).** The single-snapshot catalog looked up
   the window class of every visible window on the desktop. It now looks up
   only windows owned by processes on the allow-list.

7. **Findings from an independent review of this branch**, all fixed:
   - End Remote Session could close the stream with only the outlet
     verified. It now requires `display_status_enabled` (configuration and
     skill availability) and a verified physical display.
   - Cancelling after a mutation was sent reported `cancelled`; it now
     reports `partial`.
   - Sunshine ensure now commits under the generation lock with the full
     budget, like every other mutation.
   - The session-end verification window is now bounded per poll.
   - An OLED read failure no longer blocks the primary outlet, and it is
     reported as unknown, not as changed.
   - Mutation results must be internally consistent (`accepted` agrees with
     the observation, and an accepted result carries no `error_code`).
   - The Windows helper:
     - cross-checks `<currentgame>` against `<state>`;
     - treats an unidentifiable Sunshine process as unknown;
     - refuses a credential file whose owner is not SYSTEM or Administrators,
       or whose ACL inherits;
     - finishes inside the broker's 25 s SSH timeout.
   - "end the remote session and switch to gaming mode" now asks for
     clarification instead of running half the request.

Reviewed with no change needed: monitor acknowledgement requires both
`broker_ok` and `accepted`; mode generation and the monitor commit share one
lock; partial results are never auto-reversed; the Admin mode endpoint
freezes a server-named plan behind FRESH passkey authentication; Local never
queries or restarts Sunshine; a ready Sunshine instance is reused rather than
restarted.

## Mode behavior

### Gaming / Engineering

1. The usual online/wake check, then the SSH management path.
2. `desktop.sunshine_status` runs the interactive probe.
3. If `virtual_display_ready` is not true and
   `virtual_display_activation_enabled` is set,
   `desktop.virtual_display_activate` is sent under the generation lock. It
   must report `accepted`, `virtual_display_active`, and
   `virtual_mode_verified` (2560×1440@60), and a fresh Sunshine probe must then
   show `virtual_display_ready`. If any of those fails, the workflow stops: no
   outlet is touched, and the result is `partial`, because the activation was
   sent.
4. If MTT is already ready, it is never re-activated, so an existing stream's
   capture target stays as it is.
5. Sunshine is ensured only if it is still not ready. Both outlets turn off
   only after virtual capture is verified, and capture is verified again
   afterwards.

With activation disabled, behavior is the same as PR #6: the workflow fails
closed with `virtual_display_unavailable`.

### Local

1. Online/wake.
2. `desktop.primary_monitor_on` turns on **only** `switch.desktop_gigabyte`.
   The OLED is read and reported (`preserved_entity_unchanged`) but never
   appears in a request body.
3. If `display_status_enabled` is set, the workflow polls
   `desktop.display_status` for up to 30 s until Windows reports an active
   physical display. One active display is enough. If none appears, the
   result is `physical_display_unverified` (partial), and nothing else
   happens.
4. MTT is considered only after that, and only if
   `virtual_display_deactivation_enabled` is set (configuration refuses it
   without both observations). It is removed only when the physical display
   is verified **and** the Sunshine session state is `terminated`. Any other
   state (`active`, `resumable`, `ending`, `unknown`, unobservable, or too
   little time left) keeps MTT and is reported in
   `virtual_display_retained_reason`.
5. A failed removal does not fail Local, because physical access is already
   verified. It is reported as `deactivation_failed` with a warning.

Local never ends a stream, and never queries, starts, or restarts Sunshine's
service.

### End Remote Session → Local (explicit, separate)

This is the skill `end_remote_desktop_session`, which needs FRESH passkey
authentication and an explicit request. It is exposed at
`POST /api/admin/tools/desktop/end-remote-session`, as an Admin button with a
confirmation panel, and by the phrase "end the remote session". It shares the
mode generation, so a newer explicit request supersedes it.

1. If session status or session end is disabled, it fails before any side
   effect.
2. Physical access is restored and verified exactly as in Local. If that
   fails, nothing is closed.
3. The session state is observed. If it is `unknown`, nothing is closed. If it
   is `terminated`, the close is skipped.
4. `desktop.sunshine_session_end` closes the application. The workflow then
   polls the session state for up to 15 s until it is `terminated`. If it is
   not, the result is `remote_session_not_terminated`, and MTT is kept.
5. MTT removal follows the Local rule, re-observing the session state first.

## Sunshine Resume and session state (from Sunshine source)

The following was read from LizardByte Sunshine at the installed tag
`v2026.516.143833` (commit `14ffa6fd`):

- `nvhttp::serverinfo` reports `proc::proc.running()` as `<currentgame>` on both
  the HTTP and HTTPS ports. It is 0 exactly when no application exists.
- `nvhttp::resume` returns 503 when `running() == 0`. Otherwise it resumes
  **without** `proc_t::execute`, so no app `prep-cmd` and no `global_prep_cmd`
  runs. The pre-stream guard in `aa7648a` therefore protects `/launch` but not
  Resume. Resume calls `display_device::configure_display`, which is a no-op
  under `dd_configuration_option = disabled`.
- The UDP video, control, and audio sockets (47998/47999/48000) are bound by
  the reference-counted broadcast context only while a streaming session holds
  it.
- `POST /api/apps/close` requires HTTP Basic auth. It then runs the CSRF check,
  which passes a request that carries neither `Origin` nor `Referer`, and then
  calls `proc::proc.terminate()`. Sunshine's control thread then ends every
  session on that application within about 150 ms.
- No `/api` endpoint reports the running app or the session count.
  `/api/clients/list` lists paired devices, not connections.

What can be distinguished reliably:

| Signal | `currentgame` | stream socket bound | state |
| --- | --- | --- | --- |
| no application | 0 | no | `terminated` |
| application, no client | ≠0 | no | `resumable` |
| application, client streaming | ≠0 | yes | `active` |
| application gone, session closing | 0 | yes | `ending` |
| either read failed | — | — | `unknown` |

`terminated` comes from authoritative Sunshine state, and it is the only state
that permits removing MTT. Active versus resumable relies on the
socket-binding lifecycle and needs live confirmation. That distinction does not
affect safety, because both keep MTT. Once MTT is removed, no application
exists, so Moonlight can only Launch, which the pre-stream guard checks. A
session that cannot be established always keeps MTT, so a Resume can never
silently land on a physical display through a Butters action.

The broker rejects any session report whose fields contradict its state. For
example, it rejects `terminated` with a running app, and `unknown` with any
observation in it.

## Windows integration contract for `aa7648a`

The broker sends these as `-Operation <Name>` to `desktop-control.ps1`. The
PowerShell for the three display operations is **deliberately not written
here**, so that it does not conflict with `fix/mtt-virtual-display-activation`
(`aa7648a`). Until they are wired in, `ValidateSet` rejects them, ssh exits
non-zero, and the broker fails closed.

Each operation prints exactly one JSON object with exactly these keys
(enforced by `broker._desktop_control_result`):

- `DisplayStatus` (read-only). These five booleans make up `DISPLAY` in the
  next two operations:
  - `virtual_display_active`: the MTT identity (instance path + EDID) is active
    with a source.
  - `virtual_mode_verified`: MTT's current mode is 2560×1440@60. This implies
    `virtual_display_active`.
  - `primary_physical_active`: the G27QC identity is active and available.
  - `secondary_physical_active`: the FO32U2 identity is active and available.
  - `physical_display_active`: must equal primary OR secondary.
- `VirtualDisplayActivate`: `DISPLAY` plus `accepted`, `error_code`,
  `elapsed_ms` (int), and `already_active`.
  - Idempotent.
  - Never changes a physical display's topology.
  - `accepted` only if, afterwards, MTT is active and the mode is verified.
- `VirtualDisplayDeactivate`: `DISPLAY` plus `accepted`, `error_code`,
  `elapsed_ms`, `already_inactive`, and `session_state`. `session_state` is
  the state the helper re-checked; the broker rejects an accepted removal
  unless it is `terminated`.
  - Immediately before acting, it **must** dot-source `sunshine-session.ps1`
    and refuse with `sunshine_session_present` unless
    `(Get-ButtersSunshineSessionState).session_state -eq 'terminated'`.
  - It must refuse with `physical_display_unavailable` unless a physical
    display is active.
  - `accepted` only if, afterwards, MTT is inactive and a physical display is
    active.

Error codes that cross the broker: `virtual_display_unavailable`,
`virtual_display_identity_mismatch`, `virtual_display_mode_unverified`,
`physical_display_unavailable`, `sunshine_session_present`. Display queries are
session-scoped, so they must run in the interactive session, using the same
pattern as `\Butters\SunshineCaptureProbe`.

To integrate on Windows:

1. Push `fix/mtt-virtual-display-activation` as is.
2. Rebase it onto this branch, or merge the two.
3. Resolve the expected conflicts:
   - the `ValidateSet` line;
   - the moved `display-topology.ps1` dot-source. Any `aa7648a` operation
     that uses topology functions must dot-source the helper in its own
     `switch` case, as `SunshineProbeInteractive` now does;
   - `install-desktop-control.ps1`'s file list.
4. Map the three operations onto the existing helper using the schemas above.
5. Add the contract tests that
   `test_unpublished_mtt_operations_are_not_reimplemented_here` currently
   guards against.

## Remaining limits

- No PowerShell runtime exists on the Pi. `sunshine-session.ps1` and the
  `desktop-control.ps1` changes are covered by source-contract tests only and
  have never executed.
- Physical-display "usable" means active in the Windows topology. It does not
  prove that the panel is on its input or visible to a person.
- There is still no authoritative "person present at the desk" signal. Local is
  an explicit request, not detection.
