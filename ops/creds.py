"""Secrets the orchestrator holds, encrypted at rest with `systemd-creds --user` (ADR 0006).

`secret_store` in ops/tiers.yaml picks where they live:
- files: the old plain files under ops/env (until the human has imported them);
- creds: ~/.config/talent-scout-ops/secrets/<name>.cred, readable only after systemd decrypts it for this
  user on this machine. A plain copy left under ops/env is refused, so the move can't stay half done.

What this protects against: a secret leaking through a backup, a `cat`, a grep or a wrong `git add`. What
it does not: someone already running as this user on this machine can decrypt too. The short-lived GitHub
App tokens (ops/identity.py) are the stronger part.
"""
import os
import subprocess
from pathlib import Path

OPS = Path(__file__).resolve().parent
SYSTEMD_CREDS = os.environ.get("OPS_SYSTEMD_CREDS", "systemd-creds")
# Every secret the orchestrator reads, and where its plain file used to live.
NAMES = {
    "claude-oauth-token": OPS / "env" / "claude.oauth-token",
    "agent-gh-token": OPS / "env" / "agent.gh-token",
    "client-env-agent": OPS / "env" / "client.env.agent",
    "agent-app-key": None,
    "orchestrator-app-key": None,
}


class SecretMissing(Exception):
    pass


def store():
    if os.environ.get("OPS_SECRETS_DIR"):
        return Path(os.environ["OPS_SECRETS_DIR"])
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config) / "talent-scout-ops" / "secrets"


def path(name):
    if name not in NAMES:
        raise SecretMissing(f"unknown secret {name}; known: {', '.join(NAMES)}")
    return store() / f"{name}.cred"


def mode(cfg):
    value = cfg.get("secret_store", "files")
    if value not in ("files", "creds"):
        raise SecretMissing(f"secret_store must be files or creds, not {value!r}")
    return value


def read(name, cfg):
    """The secret's text. In creds mode, only the encrypted copy counts."""
    legacy = NAMES.get(name) if name in NAMES else None
    encrypted = path(name)
    if mode(cfg) == "creds":
        if legacy and legacy.exists():
            raise SecretMissing(f"{legacy} is still a plain file; `ops/creds import {name} {legacy} --remove` "
                                "moves it into the encrypted store")
        if not encrypted.exists():
            raise SecretMissing(f"no {name} in {store()}; add it with `ops/creds import {name} FILE`")
        done = subprocess.run([SYSTEMD_CREDS, "--user", "decrypt", f"--name={name}", str(encrypted), "-"],
                              capture_output=True)
        if done.returncode:
            raise SecretMissing(f"systemd-creds could not decrypt {name}: {done.stderr.decode().strip()[-200:]}")
        return done.stdout.decode()
    if legacy and legacy.exists():
        return legacy.read_text()
    raise SecretMissing(f"no plain file for {name}" + (f" at {legacy}" if legacy else
                                                        "; this secret needs secret_store: creds"))


def write(name, data):
    """Encrypt data into the store, then decrypt it back to prove the round trip before anyone relies on it."""
    target = path(name)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    tmp = target.with_suffix(".tmp")
    done = subprocess.run([SYSTEMD_CREDS, "--user", "encrypt", f"--name={name}", "-", str(tmp)],
                          input=data, capture_output=True)
    if done.returncode:
        tmp.unlink(missing_ok=True)
        raise SecretMissing(f"systemd-creds could not encrypt {name}: {done.stderr.decode().strip()[-200:]}")
    tmp.chmod(0o600)
    back = subprocess.run([SYSTEMD_CREDS, "--user", "decrypt", f"--name={name}", str(tmp), "-"], capture_output=True)
    if back.returncode or back.stdout != data:
        tmp.unlink(missing_ok=True)
        raise SecretMissing(f"{name} did not decrypt back to the same bytes; nothing was stored")
    tmp.replace(target)
    return target
