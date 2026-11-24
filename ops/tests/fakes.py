"""Test doubles for the sandbox: a podman that runs the command on the host with only its env file, and
human repositories with node worktrees. Every agent run and check goes through the sandbox (ADR 0002)."""
import os
import stat
import subprocess
import sys
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OPS))
import container

FAKE_PODMAN = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
log = Path(__file__).with_name("podman.log")
args = sys.argv[1:]
with open(log, "a") as out:
    out.write(" ".join(args) + "\n")
if args[:2] == ["image", "exists"]:
    sys.exit(0 if args[2] not in MISSING else 1)
if args[:1] == ["logs"]:
    proxy = Path(__file__).with_name("proxy.log")
    sys.stdout.write(proxy.read_text() if proxy.exists() else "")
    sys.exit(0)
if args[:1] != ["run"] or "-d" in args:
    sys.exit(0)
tag = next(i for i, a in enumerate(args) if a.startswith("localhost/talent-scout-"))
env, workdir = {"PATH": os.environ["PATH"]}, None
for i, a in enumerate(args[:tag]):
    if a == "--env-file":
        for line in Path(args[i + 1]).read_text().splitlines():
            key, _, value = line.partition("=")
            env[key] = value
    if a == "-w":
        workdir = args[i + 1]
if workdir:
    os.chdir(workdir)
os.execvpe(args[tag + 1], args[tag + 1:], env)
'''


def fake_podman(tmp, missing=()):
    """Install a fake podman in tmp; returns (path, log path)."""
    script = Path(tmp, "podman")
    script.write_text(FAKE_PODMAN.replace("MISSING", repr(set(missing))))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, Path(tmp, "podman.log")


def git_worktree(root, name="server-9"):
    """A human repository with origin/main and a node worktree, like prepare() makes; clones go under root."""
    root = Path(root)
    checkout = root / f"talent-scout-{name.split('-')[0]}"
    if not checkout.exists():
        checkout.mkdir(parents=True)
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "h@example.com"),
                     ("config", "user.name", "h")):
            subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True)
        (checkout / "a.txt").write_text("base\n")
        subprocess.run(["git", "add", "-A"], cwd=checkout, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=checkout, check=True, capture_output=True)
        subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=checkout, check=True)
    wt = root / "wt" / name
    if not wt.exists():
        subprocess.run(["git", "worktree", "add", "-q", str(wt), "-b", f"node/{name}", "--no-track", "origin/main"],
                       cwd=checkout, check=True, capture_output=True)
    container.CLONES = root / "clones"
    return wt


def sandbox_for_tests(tmp):
    """Point the sandbox at fakes under tmp: podman, proxy dir, clones and a Claude token."""
    # Tests write their audit records here, never into the human's ledger.
    os.environ["OPS_LEDGER_DIR"] = str(Path(tmp, "ledger"))
    container.PODMAN, _ = fake_podman(tmp)
    container.EGRESS_DIR = Path(tmp, "egress")
    container.CLONES = Path(tmp, "clones")
    token = Path(tmp, "claude.oauth-token")
    token.write_text("sk-ant-oat01-test\n")
    container.CLAUDE_TOKEN = token
