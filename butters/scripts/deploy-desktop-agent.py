"""Reviewed one-host deployment; backups before replacements, no service restarts.

Run with /opt/butters/.venv/bin/python as root from this repository.
Does not touch environmental services or browser/Tailscale authentication.
"""

import grp
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib


def main():
    if os.geteuid() != 0:
        raise SystemExit("root required")
    source = Path(__file__).resolve().parents[1]
    destination = Path("/opt/butters")
    backup = Path(tempfile.mkdtemp(prefix="butters-agent.", dir="/var/backups"))
    gid = grp.getgrnam("butters").gr_gid
    files = [
        "src/butters/actions/compute.py", "src/butters/actions/agent.py",
        "src/butters/actions/agent_ingress.py", "src/butters/actions/streaming.py",
        "src/butters/skills/desktop_agent.py", "src/butters/web/app.py",
        "src/butters/web/service.py", "src/butters/web/static/admin.html",
        "src/butters/web/static/assets/admin.js", "src/butters/actions/broker.py",
        "src/butters/actions/broker_main.py", "src/butters/assistant_config.py",
    ]
    created = []
    def save(path):
        target = backup / str(path).lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, target)
        else:
            created.append(str(path))
    for relative in files:
        target = destination / relative
        save(target)
        shutil.copyfile(source / relative, target)
        os.chown(target, 0, gid)
        target.chmod(0o640)
    # Preserve the two unrelated live/source summary differences; source stays untouched.
    service = destination / "src/butters/web/service.py"
    text = service.read_text()
    text = "\n".join(line for line in text.split("\n") if not line.strip().startswith(
        ('"ensure_parsec_running":', '"restart_parsec":')))
    service.write_text(text)
    broker_config = Path("/etc/butters/action-broker.toml")
    save(broker_config)
    config = tomllib.loads(broker_config.read_text())
    known = Path(config["desktop"]["key"]).parent / "known_hosts"
    save(known)
    # Reuse existing verified host keys; never use an unauthenticated keyscan.
    existing = subprocess.run(["ssh-keygen", "-F", config["desktop"]["host"], "-f", str(known)],
        check=True, capture_output=True, text=True).stdout
    pins = [line.split() for line in existing.splitlines() if line and not line.startswith("#")]
    if not pins:
        raise SystemExit("Existing broker host pin missing; backups retained")
    with known.open("a") as handle:
        for pin in pins:
            handle.write("DESKTOP-G4CFVL1 " + " ".join(pin[1:]) + "\n")
    text = broker_config.read_text().replace('host = "192.168.1.209"', 'host = "DESKTOP-G4CFVL1"')
    # Add only these two enumerated gates to the existing operations table.
    for operation in ("desktop.parsec_status", "desktop.parsec_ensure"):
        if operation not in config.get("operations", {}):
            text = text.replace("[operations]", '[operations]\n"' + operation + '" = true')
        elif not config["operations"][operation]:
            text = text.replace('"' + operation + '" = false', '"' + operation + '" = true')
    if "desktop.parsec_restart" not in config.get("operations", {}):
        text = text.replace("[operations]", '[operations]\n"desktop.parsec_restart" = false')
    broker_config.write_text(text)
    assistant = destination / "config/assistant.toml"
    save(assistant)
    text = assistant.read_text().replace('host = "192.168.1.209"', 'host = "DESKTOP-G4CFVL1"')
    for flag in ("parsec_status_enabled", "parsec_ensure_enabled"):
        if flag not in text:
            text = text.replace("[desktop]", "[desktop]\n" + flag + " = true")
        else:
            text = text.replace(flag + " = false", flag + " = true")
    assistant.write_text(text)
    unit = Path("/etc/systemd/system/butters-agent-ingress.service")
    save(unit)
    shutil.copyfile(source / "config/butters-agent-ingress.service", unit)
    unit.chmod(0o644)
    (backup / "created-files.txt").write_text("\n".join(created) + "\n")
    print("Backups:", backup)
    print("Installed agent integration and Parsec status/ensure gates; no services restarted")


if __name__ == "__main__":
    main()
