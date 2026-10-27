# Desktop access modes: implementation and deferred validation

## Safety and scope

This branch is source-only. It must not be deployed while the Windows development-tool installation is active. No implementation or test step requires a live Windows connection, a Home Assistant service call, monitor power, display reconfiguration, service restart, session transition, or VM workload.

The isolated branch starts at draft PR #4 commits `5217a5f` and `560a725`, retaining its verified Home Assistant convergence acknowledgement and SSH-or-ICMP post-WOL logic. The implementation changes below are layered on that work rather than duplicating it.

### Evidence classification

- **Observed in this implementation phase:** source/configuration in the isolated worktree, the prior investigation's recorded 107-second WOL-to-agent interval, deterministic fake-adapter results, and Pi-side regression tests.
- **Inferred from those observations:** 120 seconds is a reasonable initial wake-readiness window, and 180 seconds provides a bounded whole-workflow budget. These are deliberately marked for live staging validation.
- **Changed in source only:** access-mode orchestration, error/result contracts, broker schemas, the Windows helper package, Admin controls/caching, Desktop Agent catalog observation, tests, and this document.
- **Not observed or changed:** live Windows services/processes/session/display, active installers, monitor outlets, Home Assistant, production Butters, the G: volume, VMs, or the live Vivado/VLM/license state.

The implementation keeps these independent observations:

1. desktop power/online evidence (authenticated agent, SSH, and ICMP);
2. Desktop Agent freshness and interactive-session report;
3. Sunshine service readiness (cheap service/process/listener read);
4. Sunshine capture readiness (explicit interactive DXGI probe);
5. configured virtual-display readiness;
6. verified Home Assistant state for each physical monitor outlet; and
7. the requested access-mode transition.

An authenticated fresh agent heartbeat is stronger online evidence than ICMP. It does not prove SSH, Sunshine, capture, the virtual display, or an active Moonlight stream.

## State machine

All transitions begin only after an explicit, freshly authenticated request. Connectivity changes never select a mode.

```text
REQUEST(mode)
  -> OBSERVE_ONLINE [agent | SSH | ICMP]
  -> WAKE_SENT -> STARTING -> ONLINE       (when no evidence initially)
                              |-> STARTUP_TIMED_OUT

ONLINE + LOCAL
  -> VERIFY_MONITORS_ON -> READY
                       |-> PARTIAL/FAILED

ONLINE + GAMING/ENGINEERING
  -> REQUIRE_PROVISIONED_SUNSHINE_MANAGEMENT
  -> OBSERVE_SERVICE/CAPTURE/VIRTUAL_DISPLAY
  -> REUSE_READY_SUNSHINE | ENSURE_IF_NOT_READY
  -> VERIFY_MONITORS_OFF
  -> REVERIFY_CAPTURE/VIRTUAL_DISPLAY
  -> READY | PARTIAL/FAILED
```

Cancellation is checked before side effects and at bounded waits. The result always retains completed observations and transitions. If one outlet changes and the other does not, or Sunshine becomes unready after both outlets turn off, Butters reports a partial result and does not automatically reverse the outlet state. Recovery is another explicit request after the dependency is inspected.

The 120-second post-WOL readiness window is based on the observed 107-second interval between WOL and the next authenticated agent connection. The 180-second whole-workflow ceiling leaves up to 60 seconds for the fixed Sunshine observation/ensure and the broker's bounded 25-second monitor convergence operation. These values require live staging validation after installations finish.

## Mode contracts

### Gaming

- Sunshine is the only enabled backend.
- A ready service, capture probe, and configured virtual display are all required.
- A ready instance is reused; no restart is requested.
- Both monitor outlets must verify `off`.
- The implementation does not inspect or terminate Moonlight sessions and does not modify display topology.

Known limitation: the current Sunshine control helper does not expose an authoritative active-client/session signal. Session preservation is therefore achieved by never restarting a ready Sunshine instance, never reconfiguring its display, and never auto-reversing monitors—not by claiming that an active Moonlight client was observed.

### Engineering

- Uses the same Sunshine backend while the host remains Windows 11 Home.
- Returns the client profile name `engineering_low_bandwidth`.
- Initial Moonlight client guidance: use a resolution appropriate to the source text, begin at 30 FPS, and reduce the client bitrate until static applications remain clear. These are client-side settings, not host display changes.
- Both monitor outlets must verify `off`.

`desktop.engineering_backend` is the extension point for a future backend selector. It is fixed to `sunshine`; the separate `rdp_backend_enabled` and `remote_app_backend_enabled` gates default false, and selecting an unprovisioned backend fails without falling back or changing modes. A future adapter must implement separate service, session, capture/application, and health observations. It must not use patched RDP binaries or weaken Windows session isolation. Winflector is a candidate for an isolated, reversible staging experiment only; validate vendor support, macOS client behavior, graphical Vivado compatibility, licensing, security updates, clipboard/file boundaries, and resource use before adding an adapter.

### Local

- Wake only when no online evidence exists.
- Both monitor outlets must verify `on`.
- Does not query, start, stop, or restart Sunshine/Parsec.
- Does not change a Windows session or display topology.

## Low-overhead observations

- The 15-second authenticated agent heartbeat is unchanged.
- `desktop.app.list` takes one bounded process/owner snapshot and one Win32 window enumeration for the complete allowlist. Individual status and launch checks remain on demand.
- Admin Overview requests desktop summary without querying the application catalog. The Desktop panel requests the catalog and caches the result for the configured freshness interval, returning `observed_at`, `age_seconds`, and `cached`.
- General desktop status uses the new cheap Sunshine service operation. Capture/DXGI probing occurs only during explicit Gaming or Engineering preparation.
- No screen capture, encoding, GPU polling, VM inventory, continuous process enumeration, or new background subprocess was added.

No idle CPU, memory, GPU, or network number is claimed: this branch was deliberately not measured on the installing desktop. The safe post-install measurement is a short Windows Performance Recorder/Task Manager observation of the existing heartbeat plus one on-demand catalog request, compared with an idle baseline, without starting a stream or installer-affecting workload.

## Future development SSD layout

The dedicated volume is expected to be `G:` with label `VM_Development`. No directory is created by this branch. Before any provisioning, perform one read-only validation of drive letter, volume label, file system, capacity, free space, health, and existing directory contents. Stop if the identity differs. Never format, repartition, migrate, or overwrite existing data.

Reserved paths:

```text
G:\VMs\
G:\Development\
G:\Development\FPGA\Vivado\
G:\Development\Embedded\
G:\Development\HomeNetwork\
G:\ISOs\
G:\VM_Exports\
```

Keep the native Vivado installation in place. Use `G:\Development\FPGA\Vivado\` for future projects and generated artifacts, not application relocation. The SSD is not a backup; separately protect source, VM configuration, and irreplaceable guest data.

VMware Workstation Pro is the initial candidate after platform validation. Broadcom currently lists Windows 11 as a supported Workstation host and says current Workstation Pro releases are free for personal, educational, and commercial use. Microsoft documents that Client Hyper-V is unavailable on Windows 11 Home, but firmware AMD-V and interactions with Windows security virtualization still require read-only validation before installation. Put each VM's configuration and dynamically allocated virtual disks under `G:\VMs\<name>\`, installation media under `G:\ISOs\`, and exports under `G:\VM_Exports\`. Disable automatic VM start; a VM remains off unless explicitly requested. Sources: [Broadcom licensing/download guidance](https://knowledge.broadcom.com/external/article/368667/download-and-license-information-for-vmw.html), [Broadcom supported hosts](https://knowledge.broadcom.com/external/article/315653), and [Microsoft Hyper-V requirements](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/get-started/Install-Hyper-V).

## Vivado 2026.1 licensing assessment

The repository investigation cannot determine the live failure category without touching the installing desktop, so version, installed edition, VLM state, license visibility, validity, host binding, and feature-denial messages remain **unverified**.

AMD's 2026.1 documentation establishes:

- Vivado 2026.1 requires a valid accessible tier license at launch. BASIC is free but requires an annual license file. License discovery can use VLM or `XILINXD_LICENSE_FILE`. [UG973 supported devices and features](https://docs.amd.com/r/en-US/ug973-vivado-release-notes-install-license/Supported-Devices-and-Features)
- BASIC supports all 7 Series devices and Kria SOMs, so the Nexys A7's Artix-7 and the KR260 device family are covered. BASIC provides only limited simulation and ChipScope/debug features; CORE and above provide full simulation and debug. [AMD tier and device table](https://www.amd.com/en/products/software/adaptive-socs-and-fpgas/vivado/vivado-licensing-options.html)
- A local certificate license copied through VLM is normally placed in `%APPDATA%\XilinxLicense`, which AMD tools search automatically. A floating server uses `port@server` in `XILINXD_LICENSE_FILE`. [UG973 node-locked installation](https://docs.amd.com/r/en-US/ug973-vivado-release-notes-install-license/Install-Certificate-Based-Node-Locked-License-Key-File) and [floating client path](https://docs.amd.com/r/en-US/ug973-vivado-release-notes-install-license/Serve-Client-Machines-Pointing-to-a-Floating-License)
- The AMD University Program can provide Enterprise licenses for teaching and research. [AMD licensing options](https://www.amd.com/en/products/software/adaptive-socs-and-fpgas/vivado/vivado-licensing-options.html)

Minimum likely correction, subject to the deferred checks: generate the free BASIC entitlement, install the node-locked license using VLM, and confirm it is visible and valid for the machine. Do not change environment variables if the default VLM location works. BASIC is sufficient for ordinary synthesis/implementation for Nexys A7 and KR260. It is not yet proven sufficient for the user's required high-speed serial debug flow because AMD labels BASIC debug as limited; identify the exact required ILA/IBERT/Serial I/O Analyzer feature in the VLM denial before choosing a paid tier. A school-provided Enterprise license is the preferred escalation when full simulation/debug or licensed IP is genuinely required.

After installation completes, use a read-only checklist and redact sensitive values:

1. Record `vivado -version` output and installed edition/tier without launching a project flow.
2. Open VLM only if doing so does not disturb an existing Vivado process; record visible product, tier, status, expiry, and feature names. Do not record host IDs or full license text.
3. Record whether `XILINXD_LICENSE_FILE` exists and only the path type (local file/directory or floating server), not server identity or contents.
4. Confirm whether the license is absent, expired/not-yet-valid, host-mismatched, not discovered, or missing a named feature.
5. Map any denied feature to the AMD tier table and compare with an available school Enterprise entitlement before purchasing anything.

## Deployment gate and rollback

Safe now: source review, fake-adapter/unit/web tests in the isolated Pi worktree, strict-schema validation, and documentation. Deferred until every installer has finished: read-only G: identity/capacity checks, Vivado/VLM assessment, short idle-overhead measurement, staging the matched helper/broker versions, Sunshine service/capture/VDD checks, monitor outlet convergence, delayed-WOL timing, active-Moonlight preservation, and any VMware/Winflector experiment. VM provisioning and product installation require separate authorization.

Before deployment, complete code review, stage the broker and Windows helper as a matched version, provision the new enumerated `desktop.sunshine_service_status` operation, and run the deferred live validation with no installer active. Deploying only one side would make the strict broker schema reject Sunshine results.

Rollback is source/config rollback to the prior release. Do not compensate by restarting Windows, Sunshine, the Desktop Agent, networking, displays, or Home Assistant. A partially changed monitor state is left visible in the structured result for an explicit operator decision.

## Isolated verification

Run from the Pi-side temporary worktree with fake adapters and no live desktop/HA endpoints:

- `1976 passed, 2 skipped` for the complete Butters and Desktop Agent suites excluding four files whose local socket operations are blocked by the workspace sandbox;
- `82 passed` when those four local Unix/loopback socket files were run separately with only the socket sandbox restriction lifted; and
- `139 passed` in the final focused routing, access-mode, Admin parity, and compound-action check after the explicit Local-mode compatibility routing update.

`python -m compileall` and `git diff --check` also passed. The only socket-run output was four upstream deprecation warnings from the WebSockets/Uvicorn test stack. No Windows, Home Assistant, monitor, or production network endpoint was used.
