#!/usr/bin/env bash
# Gated Butters deployment of the KR260 JTAG recovery candidate (PR #9 minus
# 1e36895). Prepared 2026-10-30; nothing here runs by itself.
#
#   ./butters-candidate-deploy.sh precheck              read-only; must pass
#   ./butters-candidate-deploy.sh backup                writes /var/backups only
#   ./butters-candidate-deploy.sh deploy --authorized   install-beta1 --start + broker restart
#   ./butters-candidate-deploy.sh postcheck             read-only
#   ./butters-candidate-deploy.sh rollback --authorized swap /opt/butters.previous back
#
# Gates after deploy (nothing here edits them): kr260.status=true,
# kr260.reboot=false, desktop.wake=false, actions.kr260_jtag_recovery absent
# (disabled), actions.kr260_reboot absent (disabled). install-beta1 refuses on
# its own if any of the 50 declared approvals would change; never pass
# --allow-gate-changes.
#
# Services: install-beta1 restarts butters-web (the Desktop Agent's WebSocket
# drops and reconnects; AgentHub lives in butters-web). This script then
# restarts butters-action-broker, whose telemetry command gained the FPGA lines.
# butters-agent-ingress (TLS proxy, unchanged) and butters-live are untouched.
# Nothing on Windows or the KR260 is changed.
set -Eeuo pipefail

CANDIDATE=040ca840d492941f1e7cfeb057caf4c92c2328cb
CANDIDATE_BRANCH=deploy/kr260-jtag-recovery-candidate
PROD=ba675b40f4b5faa4d424d42a5d41cce965d6a287
PROD_BRANCH=deploy/kr260-management-recovery
PROD_DIGEST=c771a17e01ad5e543fe99802836f8e194c35705c73d69658f6cc7509d234c8ef
EXCLUDED=1e36895
PR9_HEAD=587067b
WT=/home/dmejiame/worktrees/kr260-management-recovery-deploy
BROKER_TOML_SHA=3df6ae0671a553c99c524c586e588a7a8afcacecaffb7d20948b40fa7f74fd3b
OVERLAY_SHA=1568725672829335b771a1c3b477bd383b5f11d28c3e02f77e348b7da07678c4
BOARD=192.168.1.125
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_POINTER="${HERE}/last-backup.txt"

failures=0
ok() { printf 'PASS %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; failures=$((failures + 1)); }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else bad "$name"; fi; }
finish() { if ((failures)); then echo "RESULT: ${failures} check(s) failed"; exit 1; fi; echo "RESULT: all checks passed"; }

deployed_commit() { sudo -n python3 -c 'import json;print(json.load(open("/opt/butters/DEPLOYMENT"))["commit"])'; }
recorded_digest() { sudo -n python3 -c 'import json;print(json.load(open("/opt/butters/DEPLOYMENT"))["tree_digest"])'; }
# install-beta1 sorts in the caller's locale; this Pi's is en_GB.UTF-8 (the
# recorded ba675b4 digest only reproduces with it). Try that, then C.
tree_digest() {
  local locale="$1"
  sudo -n env LOCALE="${locale}" bash -c 'cd /opt/butters && find . -xdev -type f ! -name "*.pyc" ! -path "*/__pycache__/*" ! -name DEPLOYMENT -print0 | LC_ALL="$LOCALE" sort -z | xargs -0 sha256sum | sha256sum | cut -c1-64'
}
digest_matches() {
  local want; want="$(recorded_digest)"
  [[ "$(tree_digest en_GB.UTF-8)" == "${want}" || "$(tree_digest C)" == "${want}" ]]
}
broker_gate() { sudo -n grep -E "^\"$1\"[[:space:]]*=" /etc/butters/action-broker.toml | awk -F= '{gsub(/[[:space:]]/,"",$2); print $2}'; }
file_sha() { sudo -n sha256sum "$1" | cut -c1-64; }
ready() { curl -fsS -m5 http://127.0.0.1:8090/readyz | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin)["status"]=="ready" else 1)'; }
board() {
  sudo -n timeout 20 ssh -i /etc/butters/kr260/id_ed25519 -o UserKnownHostsFile=/etc/butters/kr260/known_hosts \
    -o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=5 "ubuntu@${BOARD}" "$1"
}
observe() {
  # One read-only kr260.status broker call, exactly as Butters polls it.
  sudo -n -u butters env BUTTERS_CONFIG=/opt/butters/config/assistant.toml \
    BUTTERS_LOCAL_CONFIG=/etc/butters/assistant.local.toml PYTHONPATH=/opt/butters/src \
    PYTHONDONTWRITEBYTECODE=1 /opt/butters/.venv/bin/python - <<'PY'
import json
from butters.assistant_config import load_assistant_settings
from butters.actions.broker import BrokerClient
from butters.integrations.kr260 import Kr260Observer
s = load_assistant_settings()
o = Kr260Observer(s.kr260, broker=BrokerClient(s.broker)).observe(refresh=True)
a = s.actions
jtag = getattr(a, "kr260_jtag_recovery", None)
print(json.dumps({
    "state": o.get("operational_state"), "verified": o.get("identity_verified"),
    "boot_id": o.get("boot_id"), "fpga": (o.get("fpga") or {}).get("state"),
    "jtag_gate": [jtag.enabled, jtag.configured] if jtag else "absent",
    "reboot_gate": [a.kr260_reboot.enabled, a.kr260_reboot.configured],
    "kr260_monitoring": s.kr260.enabled,
}))
PY
}
excluded_exactly() {
  git -C "${WT}" cat-file -e "${CANDIDATE}" && python3 - "${WT}" "${CANDIDATE}" "${PR9_HEAD}" "${EXCLUDED}" <<'PY'
import collections, subprocess, sys
wt, cand, pr9, excluded = sys.argv[1:]
def lines(*cmd):
    out = subprocess.run(["git", "-C", wt, *cmd], capture_output=True, text=True, check=True).stdout
    p, m = collections.Counter(), collections.Counter()
    for l in out.splitlines():
        if l.startswith(("+++", "---")): continue
        if l.startswith("+"): p[l[1:]] += 1
        elif l.startswith("-"): m[l[1:]] += 1
    return p, m
sys.exit(0 if lines("diff", "-U0", cand, pr9) == lines("show", "-U0", "--format=", excluded) else 1)
PY
}
common_gates() {
  check "broker desktop.wake=false" test "$(broker_gate desktop.wake)" = false
  check "broker kr260.status=true" test "$(broker_gate kr260.status)" = true
  check "broker kr260.reboot=false" test "$(broker_gate kr260.reboot)" = false
  check "broker config unchanged (sha256)" test "$(file_sha /etc/butters/action-broker.toml)" = "${BROKER_TOML_SHA}"
  check "overlay unchanged (sha256)" test "$(file_sha /etc/butters/assistant.local.toml)" = "${OVERLAY_SHA}"
  check "overlay has no JTAG or restart approval" bash -c '! sudo -n grep -qE "kr260_jtag_recovery|kr260_reboot" /etc/butters/assistant.local.toml'
  for unit in butters-web butters-action-broker butters-agent-ingress; do
    check "${unit} active" systemctl is-active --quiet "${unit}"
  done
  check "/readyz ready" ready
}
board_checks() {
  local out; out="$(board 'printf "%s %s %s %s" "$(cut -c1-8 /proc/sys/kernel/random/boot_id)" "$(cat /sys/devices/system/cpu/cpu*/cpuidle/state1/disable | tr -d "\n")" "$(cat /sys/kernel/kexec_loaded)" "$(apt-mark showhold | wc -l)"' || true)"
  echo "board: ${out}"
  read -r boot idle kexec holds <<<"${out}"
  check "KR260 answers read-only SSH" test -n "${boot:-}"
  check "KR260 deep idle disabled on all CPUs" test "${idle:-}" = 1111
  check "KR260 no kexec image loaded" test "${kexec:-}" = 0
  check "KR260 six package holds" test "${holds:-}" = 6
}

precheck() {
  check "production is ${PROD:0:7}" test "$(deployed_commit)" = "${PROD}"
  check "production tree matches its recorded digest" digest_matches
  check "recorded digest is the known one" test "$(recorded_digest)" = "${PROD_DIGEST}"
  check "/opt/butters.previous exists" sudo -n test -d /opt/butters.previous
  common_gates
  check "deploy worktree clean" test -z "$(git -C "${WT}" status --porcelain)"
  check "deploy worktree has the models tree" test -d "${WT}/butters/models"
  check "candidate branch is ${CANDIDATE:0:7}" test "$(git -C "${WT}" rev-parse "${CANDIDATE_BRANCH}")" = "${CANDIDATE}"
  check "candidate descends from production" git -C "${WT}" merge-base --is-ancestor "${PROD}" "${CANDIDATE}"
  check "candidate does not contain ${EXCLUDED}" bash -c "! git -C '${WT}' merge-base --is-ancestor ${EXCLUDED} ${CANDIDATE}"
  check "candidate = PR #9 ${PR9_HEAD} minus exactly ${EXCLUDED}" excluded_exactly
  check "candidate changes no shipped config, unit or requirements" test -z "$(git -C "${WT}" diff --name-only "${PROD}" "${CANDIDATE}" -- butters/config/assistant.toml butters/systemd butters/requirements.txt)"
  board_checks
  finish
}

backup() {
  local stamp dir; stamp="$(date -u +%Y%m%dT%H%M%SZ)"; dir="/var/backups/butters-predeploy-${stamp}"
  sudo -n install -d -m 0700 -o root -g root "${dir}" "${dir}/state"
  sudo -n install -m 0600 -o root -g root /etc/butters/action-broker.toml "${dir}/action-broker.toml"
  sudo -n install -m 0640 -o root -g butters /etc/butters/assistant.local.toml "${dir}/assistant.local.toml"
  sudo -n install -m 0640 -o root -g butters /opt/butters/DEPLOYMENT "${dir}/DEPLOYMENT"
  # Consistent online copies; the databases stay in use.
  sudo -n python3 - "${dir}/state" <<'PY'
import pathlib, sqlite3, sys
out = pathlib.Path(sys.argv[1])
for db in sorted(pathlib.Path("/var/lib/butters").glob("*.sqlite3")):
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    dst = sqlite3.connect(out / db.name)
    src.backup(dst); dst.close(); src.close()
    (out / db.name).chmod(0o600)
PY
  {
    echo "# Butters predeploy backup ${stamp} (before ${CANDIDATE_BRANCH} ${CANDIDATE:0:7}; prod ${PROD:0:7})"
    echo "# after install-beta1, /opt/butters.previous holds ${PROD:0:7}"
    sudo -n bash -c "cd '${dir}' && sha256sum action-broker.toml assistant.local.toml DEPLOYMENT state/*.sqlite3"
    echo
    echo "# restore configs: install -o root -g root -m 0600 action-broker.toml /etc/butters/action-broker.toml"
    echo "#                  install -o root -g butters -m 0640 assistant.local.toml /etc/butters/assistant.local.toml"
    echo "# restore code:    $0 rollback --authorized"
  } | sudo -n tee "${dir}/MANIFEST.txt" >/dev/null
  sudo -n chmod 0600 "${dir}/MANIFEST.txt"
  echo "${dir}" > "${BACKUP_POINTER}"
  sudo -n cat "${dir}/MANIFEST.txt"
  echo "Backup: ${dir}"
}

require_authorized() {
  if [[ "${1:-}" != "--authorized" ]]; then
    echo "Refusing: '$2' changes production. Run it only with the owner's explicit approval, as: $0 $2 --authorized" >&2
    exit 2
  fi
}

deploy() {
  require_authorized "${1:-}" deploy
  local dir; dir="$(cat "${BACKUP_POINTER}" 2>/dev/null || true)"
  [[ -n "${dir}" ]] && sudo -n test -f "${dir}/MANIFEST.txt" \
    || { echo "Refusing: run '$0 backup' first." >&2; exit 2; }
  echo "== precheck"; precheck
  echo "== switching ${WT} to ${CANDIDATE_BRANCH}"
  git -C "${WT}" switch "${CANDIDATE_BRANCH}"
  test "$(git -C "${WT}" rev-parse HEAD)" = "${CANDIDATE}"
  echo "== install-beta1 --start (restarts butters-web only)"
  sudo "${WT}/butters/scripts/install-beta1" --start
  echo "== restart butters-action-broker (FPGA telemetry lines)"
  sudo -n systemctl restart butters-action-broker
  for _ in $(seq 1 30); do ready 2>/dev/null && break; sleep 2; done
  echo "== postcheck"; postcheck
}

postcheck() {
  check "production is the candidate ${CANDIDATE:0:7}" test "$(deployed_commit)" = "${CANDIDATE}"
  check "installed tree matches its recorded digest" digest_matches
  check "/opt/butters.previous is ${PROD:0:7}" test "$(sudo -n python3 -c 'import json;print(json.load(open("/opt/butters.previous/DEPLOYMENT"))["commit"])')" = "${PROD}"
  common_gates
  local since o; since="$(systemctl show -p ActiveEnterTimestamp --value butters-web)"
  check "Desktop Agent reconnected after the restart" bash -c "sudo -n journalctl -u butters-web --since '${since}' --no-pager -o cat | grep -q 'WebSocket /agent/v1/session\" \[accepted\]'"
  check "no Python traceback since the restart" bash -c "! sudo -n journalctl -u butters-web -u butters-action-broker --since '${since}' --no-pager -o cat | grep -q Traceback"
  o="$(observe 2>&1 | tail -1 || true)"; echo "observer: ${o}"
  check "KR260 online and identity-verified" grep -q '"state": "online", "verified": true' <<<"${o}"
  check "FPGA telemetry present (operating)" grep -q '"fpga": "operating"' <<<"${o}"
  check "JTAG gate disabled and unconfigured" grep -q '"jtag_gate": \[false, false\]' <<<"${o}"
  check "restart gate disabled" grep -q '"reboot_gate": \[false, false\]' <<<"${o}"
  board_checks
  finish
}

rollback() {
  require_authorized "${1:-}" rollback
  echo "Before a Butters rollback the Desktop Agent's JTAG gate must be off"
  echo "(kr260-jtag.toml enabled=false or absent), or production rejects its hello."
  test "$(sudo -n python3 -c 'import json;print(json.load(open("/opt/butters.previous/DEPLOYMENT"))["commit"])')" = "${PROD}" \
    || { echo "Refusing: /opt/butters.previous is not ${PROD:0:7}." >&2; exit 2; }
  local stamp; stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  sudo -n mv /opt/butters "/opt/butters.rolledback-${stamp}"
  sudo -n mv /opt/butters.previous /opt/butters
  sudo -n systemctl restart butters-web butters-action-broker
  for _ in $(seq 1 30); do ready 2>/dev/null && break; sleep 2; done
  check "production is ${PROD:0:7} again" test "$(deployed_commit)" = "${PROD}"
  check "restored tree matches its recorded digest" digest_matches
  common_gates
  echo "Rolled-back candidate tree kept at /opt/butters.rolledback-${stamp}."
  echo "Optional: git -C ${WT} switch ${PROD_BRANCH}"
  finish
}

case "${1:-}" in
  precheck) precheck ;;
  backup) backup ;;
  deploy) shift; deploy "$@" ;;
  postcheck) postcheck ;;
  rollback) shift; rollback "$@" ;;
  *) sed -n 2,9p "$0"; exit 2 ;;
esac
