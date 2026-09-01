# Release Candidate Deployment Plan

Branch: `review/butters-ultracode-release-candidate`
Base: `origin/main` (`0c1d78b`, "Merge first-class Bambu telemetry release")
Merged in: `826fc9a` (Desktop Remote Management v2 continuation) and its parent `4925c64`

**This plan has not been executed. Nothing in this branch has been deployed.**

Production was audited read-only and is byte-identical to `origin/main`: all 27
`server/backend/app/*.py`, all `server/backend/bridge/*.py`, the three frontend
assets, all 117 `butters/src/**/*.py`, all four Butters units, all four
home-sensor units and the tmpfiles rule. There is no drift to reconcile before
deploying, and the whole candidate delta is the diff below.

---

## 1. Git preflight

```sh
cd /home/dmejiame/ESP32-home-sensor-hub
SSH_AUTH_SOCK=/tmp/ssh-biSmBpVVCWoe/agent.33518 git fetch origin --prune
git rev-parse origin/main                                   # expect 0c1d78b…
git rev-parse origin/review/butters-ultracode-release-candidate
git log --oneline origin/main..review/butters-ultracode-release-candidate
git diff --stat origin/main review/butters-ultracode-release-candidate
```

Refuse to continue if `origin/main` has moved, or if the review branch is not a
descendant of both `0c1d78b` and `826fc9a`.

## 2. Production-state preflight

```sh
systemctl is-active  butters-web.service butters-action-broker.socket \
                     home-sensor-dashboard.service home-sensor-bridge.service \
                     home-sensor-export-worker.service home-sensor-printer-observer.service
systemctl is-enabled butters-web.service butters-action-broker.socket \
                     home-sensor-dashboard.service home-sensor-bridge.service \
                     home-sensor-export-worker.service home-sensor-printer-observer.service
df -h /opt /var/lib                # the Butters install needs room for
                                   # /opt/butters + .previous + a staging tree
curl -fsS http://127.0.0.1:8080/api/status  | head -c 200
curl -fsS http://127.0.0.1:8090/healthz
```

`butters-live.service` is expected `disabled`/`inactive`. Leave it that way: it
claims the ALSA capture device exclusively and nothing in this candidate needs
it.

## 3. Configuration backup

```sh
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
sudo install -d -m 0700 -o root -g root /root/butters-rc-backup-$STAMP
sudo cp -a /etc/butters               /root/butters-rc-backup-$STAMP/etc-butters
sudo cp -a /etc/home-sensor           /root/butters-rc-backup-$STAMP/etc-home-sensor
sudo cp -a /etc/systemd/system/butters-*.service \
           /etc/systemd/system/butters-*.socket \
           /etc/systemd/system/home-sensor-*.service \
           /etc/tmpfiles.d/butters-action-broker.conf \
                                      /root/butters-rc-backup-$STAMP/units/
sudo chmod -R go= /root/butters-rc-backup-$STAMP
```

The backup contains secrets (`butters.env`, `printer.env`, the broker's SSH
key). It must be root-only, `0700`, and must not be written under `/opt`,
`/var/lib`, or any path an installer prunes.

## 4. Persistent-state backup

Stop nothing; SQLite is safest copied with its own tooling.

```sh
sudo -u home-sensor sqlite3 /var/lib/home-sensor/monitoring.sqlite3 \
  ".backup '/root/butters-rc-backup-$STAMP/monitoring.sqlite3'"
sudo -u home-sensor sqlite3 /var/lib/home-sensor/printer.sqlite3 \
  ".backup '/root/butters-rc-backup-$STAMP/printer.sqlite3'"
for db in state actions usage skill-jobs security; do
  sudo -u butters sqlite3 /var/lib/butters/$db.sqlite3 \
    ".backup '/root/butters-rc-backup-$STAMP/butters-$db.sqlite3'"
done
sudo chmod -R go= /root/butters-rc-backup-$STAMP
```

`printer.sqlite3` holds the tracked print-runtime accumulator, the maintenance
completion history and the notification ledger. Losing it loses the maintenance
baselines; nothing in this candidate rebuilds them.

## 5. Model preservation

`install-beta1` re-sources models from the checkout on every install and
destroys the previously deployed copy in the swap. Before deploying Butters:

```sh
ls -la /home/dmejiame/ESP32-home-sensor-hub/butters/models
```

`butters/models/` and `butters/runtime/` are gitignored, so they exist only in
the working checkout. If they are absent the installer aborts at its own guard,
which is the correct behaviour — restore them before proceeding rather than
working around it.

## 6. Migration

No schema migration. Two configuration changes, both operator decisions:

1. **`/etc/butters/action-broker.toml` `[operations]` has no `desktop.parsec_*`
   keys**, because production predates them. `broker_main` requires the exact
   key set, so add all three explicitly:

   ```toml
   "desktop.parsec_status"  = false
   "desktop.parsec_ensure"  = false
   "desktop.parsec_restart" = false
   ```

   Deploy with them `false` first. Enabling them is step 21's decision.

2. **`/opt/butters` and `/opt/home-sensor/server` gain a `RELEASE` stamp** so
   the deployed revision is visible from `/api/system-status`. Write it as part
   of the rsync in step 11.

`desktop.sleep` stays `false`. `assistant.toml` ships `sleep_enabled = false`
and all three `parsec_*_enabled = false`; do not change them in this deployment.

## 7. Server deployment

Follow `server/docs/DEPLOYMENT.md`. In summary:

```sh
cd /home/dmejiame/ESP32-home-sensor-hub
git switch review/butters-ultracode-release-candidate
sudo rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
  server/ /opt/home-sensor/server/
git rev-parse --short=12 HEAD | sudo tee /opt/home-sensor/server/RELEASE
sudo chmod 0644 /opt/home-sensor/server/RELEASE
```

New file this release: `server/backend/app/system_health.py`. No new dependency
is required by it; it uses only the standard library.

## 8. Unit installation

One unit changed: `butters-action-broker.service` gains `MemoryMax=256M` and
`TasksMax=64`. It is the only Butters unit that ran as root without a cgroup
bound, while `butters-web` and `butters-live` have carried one all along.

`install-action-broker` rewrites the unit, so run it and reload:

```sh
sudo ./butters/scripts/install-action-broker
systemctl show butters-action-broker.service -p MemoryMax,TasksMax
```

The bound applies to the next activation. The broker is socket-activated, so
stopping the service (step 10) is enough; the socket must stay up.

## 9. Enablement decision

No enablement change. Every unit that is enabled today stays enabled; nothing
new is enabled. `butters-live.service` stays disabled.

## 10. Broker update

The broker's Python changed (`butters/src/butters/actions/broker.py`). It is
socket-activated and `Restart=no`, so the running process keeps executing the
old code until it exits. After the Butters install in step 11:

```sh
sudo systemctl stop butters-action-broker.service   # the socket stays listening
systemctl is-active butters-action-broker.socket    # expect active
```

The next request re-activates it from the new tree. Do not stop the socket.

## 11. Butters deployment

```sh
cd /home/dmejiame/ESP32-home-sensor-hub
sudo ./butters/scripts/install-beta1
git rev-parse --short=12 HEAD | sudo tee /opt/butters/RELEASE
sudo chmod 0640 /opt/butters/RELEASE && sudo chown root:butters /opt/butters/RELEASE
```

`install-beta1` does not enable or start anything without `--enable`/`--start`.
Butters-web is already enabled, so pass neither; restart it explicitly in step
12. This release changes the installer itself: the swap now keeps a rollback
copy throughout, restores the previous tree if publishing fails, retargets the
venv console-script shebangs to `/opt/butters`, and sweeps stale staging trees
from earlier interrupted runs.

## 12. Restart ordering

```sh
sudo systemctl restart home-sensor-export-worker.service
sudo systemctl restart home-sensor-printer-observer.service
sudo systemctl restart home-sensor-bridge.service
sudo systemctl restart home-sensor-dashboard.service
sudo systemctl restart butters-web.service
```

Dashboard before Butters: `butters-web.service` is ordered `After=` it and reads
its API. The broker is not restarted; it re-activates on demand.

## 13. Readiness

```sh
sudo systemctl status --no-pager home-sensor-dashboard.service butters-web.service
sudo ./butters/scripts/verify-beta1
```

`verify-beta1` now guards every probe, so it reports systemd and Tailscale state
even when `/healthz` fails.

Expect `/readyz` `checks.configuration`, `state_directory` and
`deterministic_router` to be `ready` — they now exercise the real registries,
the real router and a real write, instead of returning a constant.
`action_broker` will read `unavailable` until something activates the socket;
it never gates readiness.

## 14. API smoke tests

```sh
curl -fsS http://127.0.0.1:8080/api/health
curl -fsS http://127.0.0.1:8080/api/status  | python3 -m json.tool | head -40
curl -fsS http://127.0.0.1:8080/api/latest  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["nodes"]))'
```

`/api/status` must now list **nine** services: the six from before plus
`home-sensor-printer-observer.service`, `butters-web.service` and
`butters-action-broker.socket`. Every one must report a non-null
`uptime_seconds` — that field was null for every unit before this release.

## 15. Offline-sensor regression smoke test

```sh
curl -fsS http://127.0.0.1:8080/api/latest | python3 - <<'PY'
import json,sys
d=json.load(sys.stdin)
office=[n for n in d["nodes"] if n["id"]=="office"]
assert office, "the long-offline office node disappeared from the inventory"
print(office[0]["status"], office[0]["last_seen"], office[0]["stale_reason"])
env={n["id"]: n for n in d["environment"]}
for nid in ("2","3"):
    assert env[nid]["battery_mv"] is None, "battery-disabled node reported a voltage"
    assert env[nid]["battery_measurement_ok"] is False
print("durable inventory and battery-null semantics intact")
PY
```

`office` has been offline since 2026-08-21 and must still be present with
`status=offline` and its `last_seen` retained.

## 16. Bambu telemetry smoke test

```sh
curl -fsS http://127.0.0.1:8080/api/printer | python3 -c '
import json,sys; d=json.load(sys.stdin)
print(d["printer_id"], d["normalized_state"], d["online"], d["observed_at"])
print("history import:", d["history_import"]["imported_records"], d["history_import"]["last_error"])'
```

## 17. Printer-hour smoke test

```sh
curl -fsS http://127.0.0.1:8080/api/printer/usage | python3 -c '
import json,sys; u=json.load(sys.stdin)
print("tracked_print_hours", u["tracked_print_hours"])
print("complete", u["tracked_history_complete"], u["tracked_history_completeness_reasons"])
print("printer_reported_lifetime_hours", u["printer_reported_lifetime_hours"])'
```

`tracked_print_hours` must be **greater than or equal to** the value recorded in
step 2 (453.49 at audit time). A decrease means the accumulator was rebuilt and
is a rollback trigger. `printer_reported_lifetime_hours` must stay `null`: the
printer exposes no authoritative lifetime counter and the API must not imply one.

## 18. Maintenance smoke test

```sh
curl -fsS http://127.0.0.1:8080/api/printer/maintenance | python3 -c '
import json,sys; m=json.load(sys.stdin)
print(m["summary"]["counts"], m["summary"]["maintenance_mode"])
print("completions kept:", len(m["completion_history"]))
print("source:", m["manufacturer_source"]["source"], m["manufacturer_source"]["revision"])'
```

`completion_history` must retain the two completions recorded on 2026-08-26.
Do **not** exercise the completion POST as a smoke test: it writes a real
maintenance baseline.

Cross-origin refusal can be checked without writing anything, because the origin
check runs before the body is read:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H 'Origin: http://evil.example' -H 'Content-Type: application/json' \
  -d '{}' http://127.0.0.1:8080/api/printer/maintenance/complete-all   # expect 403
```

## 19. System-health smoke test

```sh
curl -fsS http://127.0.0.1:8080/api/system-status | python3 -c '
import json,sys; h=json.load(sys.stdin)
print(h["overall_state"], h["counts"])
print("revision", h["service"]["source_revision"], h["service"]["source_revision_origin"])
print("timed out:", h["probe"]["timed_out"])
for d in h["dependencies"]: print(" ", d["dependency_id"], d["state"], d["basis"])'
```

`source_revision_origin` must be `release_file` and `source_revision` must match
the deployed commit from step 7. `probe.timed_out` must be empty.

`overall_state` will be **`degraded`**, not `healthy`, while the `office` node
remains offline. That is the correct answer and the reason the endpoint exists.

## 20. Model exposure check

```sh
cd /home/dmejiame/ESP32-home-sensor-hub
./butters/scripts/test-butters -q -k "model_tool_policy or catalog"
```

Must report ACTION-class model exposure of zero. The catalog is also enforced on
execution now, in both the local fallback and the cloud tool loop.

## 21. Desktop check — without waking Windows

Source and configuration only. **Do not send WOL, do not SSH to the desktop, do
not start Parsec, do not touch monitors.**

```sh
sudo grep -E '^"desktop\.' /etc/butters/action-broker.toml
grep -E 'parsec_.*_enabled|sleep_enabled|wake_enabled' /opt/butters/config/assistant.toml
sudo ls -l /etc/butters/action-broker/           # key + known_hosts, root:root 0600
./butters/scripts/test-butters -q -k "desktop or broker or parsec"
```

Expected: `desktop.sleep = false`, all three `desktop.parsec_*` present and
`false`, `sleep_enabled = false`.

**Operator decision, deliberately not made here:** the validated live acceptance
(shutdown → one WOL → boot → SSH → Parsec Manual/Stopped → explicit ensure →
ready) exercised Parsec. This candidate ships Parsec **disabled** at both the
assistant and broker layers. Enabling it requires, together:

* `/opt/butters/config/assistant.toml`: `parsec_status_enabled`,
  `parsec_ensure_enabled`, `parsec_restart_enabled` → `true`
* `/etc/butters/action-broker.toml`: the matching `desktop.parsec_*` → `true`
* `C:\ProgramData\Butters\desktop-control.ps1` installed on DESKTOP-G4CFVL1 via
  `install-desktop-control.ps1`

Note that enabling `parsec_status_enabled` also adds the read-only tool
`get_parsec_status` to the model catalog. Its only argument is `machine` with a
closed enum of `["desktop"]`, and it returns bounded state with no host, path or
credential — but it is a capability expansion and should be a conscious choice.

## 22. Rollback triggers

Roll back if any of these hold after step 12:

* `/api/latest` no longer lists the `office` node, or a battery-disabled node
  reports `0` instead of `null`
* `tracked_print_hours` decreased, or `completion_history` lost entries
* `/api/status` or `/api/latest` returns 5xx
* `butters-web.service` or `home-sensor-dashboard.service` restart-loops
* `/api/system-status` reports `unavailable` for a dependency that step 2 showed
  active, and systemd agrees the unit is failed
* the broker returns `operation_failed` for `desktop.wake` where it previously
  succeeded

`overall_state: degraded` caused only by the already-offline `office` node is
**not** a rollback trigger.

## 23. Rollback order

Server:

```sh
sudo systemctl stop home-sensor-dashboard.service home-sensor-export-worker.service \
                    home-sensor-printer-observer.service home-sensor-bridge.service
cd /home/dmejiame/ESP32-home-sensor-hub && git switch --detach 0c1d78b
sudo rsync -a --delete --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
  server/ /opt/home-sensor/server/
sudo rm -f /opt/home-sensor/server/RELEASE
sudo systemctl start home-sensor-bridge.service home-sensor-printer-observer.service \
                     home-sensor-export-worker.service home-sensor-dashboard.service
```

Butters:

```sh
sudo systemctl stop butters-web.service
sudo systemctl stop butters-action-broker.service
sudo rm -rf /opt/butters.rollback && sudo mv /opt/butters /opt/butters.rollback
sudo mv /opt/butters.previous /opt/butters
sudo systemctl start butters-web.service
```

Configuration, if step 6 was applied:

```sh
sudo cp -a /root/butters-rc-backup-$STAMP/etc-butters/action-broker.toml \
           /etc/butters/action-broker.toml
```

Persistent state: **do not restore the SQLite backups unless a rollback trigger
was a data defect.** Rolling back code while restoring an older
`printer.sqlite3` discards every print and maintenance event recorded since the
backup. The old code reads the current databases correctly; no migration in this
candidate changes their shape.

## 24. Proposed release tag

`release-2026-09-01-desktop-v2-observability`

Tag only after the smoke tests in steps 13–21 pass, and tag the merge commit on
`main`, not the review branch.

---

## Prerequisites and decisions still owed by a human

1. Merge `review/butters-ultracode-release-candidate` into `main` — not done here.
2. Decide whether to enable Parsec (step 21). Shipping it disabled is the safe
   default and preserves current production behaviour exactly.
3. Decide whether to install `desktop-control.ps1` on DESKTOP-G4CFVL1. It is a
   Windows change and is out of scope for this pass.
4. Sleep and hibernation stay disabled and unsupported. The separate Windows
   sleep investigation is not part of this candidate and none of its work is
   merged here.
