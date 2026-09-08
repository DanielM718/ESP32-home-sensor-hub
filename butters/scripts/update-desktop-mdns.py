"""Retain verified pins and use Windows mDNS after short DNS fails."""

import os
from pathlib import Path
import tomllib

if os.geteuid() != 0:
    raise SystemExit("root required")
broker = Path("/etc/butters/action-broker.toml")
config = tomllib.loads(broker.read_text())
if config["desktop"]["host"] not in {"DESKTOP-G4CFVL1", "DESKTOP-G4CFVL1.local"}:
    raise SystemExit("Unexpected broker target; inspect first")
known = Path(config["desktop"]["key"]).parent / "known_hosts"
lines = known.read_text().splitlines()
if not any(line.startswith("DESKTOP-G4CFVL1 ") for line in lines):
    raise SystemExit("Verified hostname pin missing; do not keyscan")
known.write_text("\n".join(line.replace("DESKTOP-G4CFVL1 ",
    "DESKTOP-G4CFVL1,DESKTOP-G4CFVL1.local ", 1)
    if line.startswith("DESKTOP-G4CFVL1 ") else line for line in lines) + "\n")
for path in (broker, Path("/opt/butters/config/assistant.toml")):
    text = path.read_text()
    path.write_text(text.replace('host = "DESKTOP-G4CFVL1"', 'host = "DESKTOP-G4CFVL1.local"'))
print("Broker/observer target now uses verified mDNS; existing host pins retained")
