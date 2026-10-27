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

The reviewed identity tuple contains all of the following:

- Sunshine device ID;
- Windows monitor device-instance path from `QueryDisplayConfig` and
  `DisplayConfigGetDeviceInfo`;
- EDID manufacturer, product, and serial observations;
- whether the target is the MTT virtual display or a physical monitor.

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
`dd_configuration_option = verify_only`. Resolution and refresh-rate topology
management are disabled. This lets Sunshine reject an unavailable target but
prevents it from owning and restoring a pre-stream topology snapshot.

Disconnect handling has one safe action: leave the current topology unchanged.
A newer Local transition, newly active physical display, changed identity set,
stale snapshot, or repeated disconnect notification never causes restoration.
Physical Tapo outlet power remains a separately observed Home Assistant state.

The desktop workflow serializes mode transitions and assigns each request a
generation. A newer Local request supersedes an older remote request before any
later monitor-off side effect; if the broker call was already in flight, Local
converges the final state to on. Broker `ok` and operation `accepted` are both
required. Fixed-host mDNS resolution runs in a killable, one-second helper so
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
