"""One-time local enrollment run as root on Butters; never prints credentials.

Creates new files only. Does not restart/deploy services or install the Windows task.
Windows package/venv must already exist; credentials are streamed over pinned SSH.
"""

import base64
from datetime import datetime, timedelta, timezone
import grp
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run as root on Butters")
    etc = Path("/etc/butters")
    targets = [etc / name for name in ("agents.toml", "agent-command.key",
        "agent-ingress.toml", "agent-server.key", "agent-server.crt")]
    if any(path.exists() for path in targets):
        raise SystemExit("Agent configuration already exists; inspect instead of overwriting")
    gid = grp.getgrnam("butters").gr_gid
    def create(path, data):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chown(path, 0, gid)

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "butters.lan")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=730))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("butters.lan")]), critical=False)
        .sign(key, hashes.SHA256()))
    pin = hashlib.sha256(key.public_key().public_bytes(serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo)).hexdigest()
    token, command_key = secrets.token_hex(32), secrets.token_hex(32)
    create(etc / "agent-server.key", key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    create(etc / "agent-server.crt", certificate.public_bytes(serialization.Encoding.PEM))
    create(etc / "agent-command.key", command_key.encode())
    create(etc / "agents.toml", ('schema_version=1\nagent_id="desktop"\ntoken_sha256="' +
        hashlib.sha256(token.encode()).hexdigest() +
        '"\ncommand_key_file="/etc/butters/agent-command.key"\n').encode())
    create(etc / "agent-ingress.toml", b'bind_interfaces=["eth0","wlan0"]\nport=8443\ncertificate="/etc/butters/agent-server.crt"\nprivate_key="/etc/butters/agent-server.key"\n')
    # Windows DPAPI is called under the operator's existing Daniel SSH identity.
    command = '"/c/ProgramData/Butters/DesktopAgent/venv/Scripts/python.exe" -m butters_agent.provision'
    result = subprocess.run(["sudo", "-u", "dmejiame", "ssh", "-o", "BatchMode=yes",
        "desktop", command], input=json.dumps({"token":token, "command_key":command_key}).encode(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if result.returncode:
        raise SystemExit("DPAPI enrollment failed; generated server files retained for explicit recovery")
    # The remaining files contain public configuration only.
    with tempfile.TemporaryDirectory(prefix="butters-agent-public-") as directory:
        os.chmod(directory, 0o755)
        config = Path(directory) / "agent.toml"
        config.write_text('schema_version=1\nagent_id="desktop"\nurl="wss://butters.lan:8443/agent/v1/session"\nspki_sha256="' + pin + '"\n')
        apps = Path(directory) / "apps.toml"
        apps.write_text(r"""schema_version=1
[apps.parsec]
path='C:\Program Files\Parsec\parsecd.exe'
images=['C:\Program Files\Parsec\parsecd.exe']
[apps.git_bash]
path='C:\Program Files\Git\git-bash.exe'
images=['C:\Program Files\Git\usr\bin\mintty.exe']
require_visible=true
[apps.vs_code]
path='C:\Users\Daniel\AppData\Local\Programs\Microsoft VS Code\Code.exe'
images=['C:\Users\Daniel\AppData\Local\Programs\Microsoft VS Code\Code.exe']
""")
        config.chmod(0o644)
        apps.chmod(0o644)
        subprocess.run(["sudo", "-u", "dmejiame", "scp", str(config), str(apps),
            "desktop:C:/ProgramData/Butters/DesktopAgent/"], check=True, timeout=30)
    print("Created server TLS identity, token hash and signing key; enrolled Windows user-scoped DPAPI credentials")


if __name__ == "__main__":
    main()
