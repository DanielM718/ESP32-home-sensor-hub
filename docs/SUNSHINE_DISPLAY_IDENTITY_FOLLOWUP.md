# Sunshine display identity follow-up

This branch is a source-only follow-up to PR #5. It has not been deployed and
must not be used to alter the live Windows display topology without the manual
validation below.

## Why the old policy is unsafe

Windows `DISPLAYn` names are enumeration aliases, not monitor identities. The
observed MTT virtual display has changed from `DISPLAY94` to `DISPLAY26`, and a
physical output can receive either number after re-enumeration. A saved display
topology that predates a local user's physical-monitor change is also stale and
must never be replayed on Moonlight disconnect.

The installed Sunshine build identifies its configured output by a stable
Sunshine device ID but reports the current source alias through `dxgi-info`.
Comparing those two values directly makes a healthy virtual display look
unavailable.

## Fixed identities

The production PowerShell identity tuple contains all of the following:

- Sunshine device ID for the configured virtual capture target;
- Windows monitor device-instance path from `QueryDisplayConfig` and
  `DisplayConfigGetDeviceInfo`;
- EDID manufacturer and product observations exposed by
  `DISPLAYCONFIG_TARGET_DEVICE_NAME`; and
- whether the target is the MTT virtual display or a physical monitor.

Sunshine's logged EDID serial is useful audit evidence, but the Windows
`DisplayConfigGetDeviceInfo` target-name packet does not expose it. The helper
therefore does not claim that serial is part of its production match.

The current fixed inventory is:

| Target | Sunshine device ID | Windows instance | EDID |
| --- | --- | --- | --- |
| MTT VDD | `{5eb52002-659f-5729-bdd8-9cdc4efd1bf5}` | `DISPLAY\MTT1337\1&15ECD195&4&UID256` | MTT / 1337 / 518463207 |
| Gigabyte G27QC | `{379ade36-a9ac-587a-b130-9bffdcab7058}` | `DISPLAY\GBT270B\5&19CEA493&0&UID4353` | GBT / 270B / 158 |
| AORUS FO32U2 | `{5d53b114-72f4-51ef-b227-befdc81192db}` | `DISPLAY\GBT3214\5&19CEA493&0&UID4357` | GBT / 3214 / serial unavailable |

Every topology-affecting operation must re-query this inventory immediately
before use. Missing, duplicate, or changed identity fails closed. The current
`DISPLAYn` name is used only after it has been bound to the stable MTT target.

## Sunshine policy

Sunshine is pinned to the MTT device ID with
`dd_configuration_option = disabled`. Resolution, refresh-rate, HDR, and
topology management are disabled. The installed `2026.516.143833` build accepts
`verify_only`, but its bundled libdisplaydevice still persists the initial
topology even when verification makes no change. `verify_only` therefore does
not prevent stale restoration; `disabled` is required to prevent a new display
snapshot from being created.

Before the first restart with this policy, use Sunshine's authenticated
`/api/reset-display-device-persistence` operation in the approved maintenance
window and verify that `display_device.state` is absent. Do not delete the file
behind a running Sunshine process: the in-memory state can still be reverted on
shutdown. The direct `sunshine.conf` edit is not live; all `dd_*` and
`output_name` changes take effect only after a Sunshine restart.

With display-device configuration disabled and persistence reset, disconnect
handling has no saved topology to restore. Physical Tapo outlet power remains a
separately observed Home Assistant state.

The desktop workflow serializes mode transitions and assigns each accepted
request a generation. Generation registration and the bounded monitor broker
call share one commit lock: a newer Local generation either supersedes an older
remote request before monitor-off is sent, or is accepted after an already
in-flight call and converges the next state to on. Broker `ok` and operation
`accepted` are both required. This ordering does not constitute local-use
detection; no authoritative physical-presence signal exists in this branch.
Fixed-host mDNS resolution runs in a killable, one-second helper so
the 120-second post-WOL window and 180-second total deadline remain wall-clock
bounds rather than best-effort socket timeouts.

## Required live validation before deployment

1. Back up `sunshine.conf`, `apps.json`, the installed Butters Windows helpers,
   and Sunshine's display-device log.
2. Run only the read-only inventory and verify exactly one MTT, G27QC, and
   FO32U2 identity. Stop on any mismatch.
3. Validate the `QueryDisplayConfig` helper on Windows without calling
   `ChangeDisplaySettingsEx`.
4. With both outlets already on, confirm Local mode reports both Home Assistant
   entities `on` and leaves Sunshine and Windows topology untouched.
5. In a supervised test window, start a stream, activate a physical display,
   end the stream, and confirm the physical display stays active. Do not run
   this while Vivado or another important workload is unattended.
6. Verify a second identical disconnect notification is a no-op and verify a
   remote request superseded by Local mode never issues monitor-off after the
   Local request.

Rollback is configuration-first: restore the backed-up Sunshine configuration
and helpers, then use the already approved monitor recovery helper to turn both
outlets on. A rollback does not authorize changing Windows topology, restarting
Windows, or restarting Sunshine during an active session.
