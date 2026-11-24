"""Who acts on GitHub (ADR 0006): two GitHub Apps instead of a personal token and the human's gh login.

- ts-agent: Issues read/write and Metadata read. Workers and reviewers get its token inside the sandbox.
- ts-orchestrator: Contents, Pull requests and Issues write; Checks and Commit statuses read. ops/run uses it
  to push, open PRs, label and comment. It has no Workflows or Administration permission, so it can't
  change CI or branch rules, and the human approves what it opens.

Every token is an installation token, minted from the App's private key for the repos and permissions this
run needs, and it expires within an hour. Tokens live in memory only. `github_identity: legacy` in
ops/tiers.yaml keeps the old way (agent PAT, human gh) until both Apps are set up.
"""
import base64
import calendar
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import creds

API = os.environ.get("OPS_GITHUB_API", "https://api.github.com")
OPENSSL = os.environ.get("OPS_OPENSSL", "openssl")
PERMISSIONS = {
    "agent": {"issues": "write", "metadata": "read"},
    "orchestrator": {"contents": "write", "pull_requests": "write", "issues": "write", "checks": "read",
                     "statuses": "read", "metadata": "read"},
}
_cache = {}


class IdentityError(Exception):
    pass


def mode(cfg):
    value = cfg.get("github_identity", "legacy")
    if value not in ("legacy", "apps"):
        raise IdentityError(f"github_identity must be legacy or apps, not {value!r}")
    return value


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id, key_pem):
    """A 9-minute JWT signed RS256 with the App's key. openssl signs, so no third-party library is needed.
    The key touches disk only in a private temp dir, on RAM-backed /dev/shm when the host has it."""
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    signing_input = f"{header}.{payload}".encode()
    base = "/dev/shm" if Path("/dev/shm").is_dir() else None
    with tempfile.TemporaryDirectory(prefix="ops-key-", dir=base) as tmp:
        key = Path(tmp, "key.pem")
        key.touch(mode=0o600)
        key.write_text(key_pem)
        done = subprocess.run([OPENSSL, "dgst", "-sha256", "-sign", str(key)], input=signing_input,
                              capture_output=True)
    if done.returncode:
        raise IdentityError(f"openssl could not sign the App JWT: {done.stderr.decode().strip()[-200:]}")
    return f"{header}.{payload}.{_b64(done.stdout)}"


def _call(method, url, bearer, body=None):
    request = urllib.request.Request(f"{API}{url}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {bearer}",
                                              "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28",
                                              "User-Agent": "talent-scout-ops"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as err:
        raise IdentityError(f"GitHub {method} {url}: {err.code} {err.read().decode()[:200]}")
    except urllib.error.URLError as err:
        raise IdentityError(f"GitHub {method} {url}: {err.reason}")


def token(role, repos, cfg):
    """An installation token for role ("agent" or "orchestrator"), limited to repos (full names) and to the
    role's permissions. Cached in memory until five minutes before it expires."""
    key = (role, tuple(sorted(repos)))
    cached = _cache.get(key)
    if cached and cached[1] - time.time() > 300:
        return cached[0]
    app_id = cfg.get(f"{role}_app_id")
    if not app_id:
        raise IdentityError(f"{role}_app_id is not set in ops/tiers.yaml; see 'GitHub Apps' in WORKFLOW.md")
    jwt = app_jwt(app_id, creds.read(f"{role}-app-key", cfg))
    owner = repos[0].split("/")[0]
    installation = _call("GET", f"/repos/{repos[0]}/installation", jwt)["id"]
    granted = _call("POST", f"/app/installations/{installation}/access_tokens", jwt,
                    {"repositories": [r.split("/")[1] for r in repos if r.split("/")[0] == owner],
                     "permissions": PERMISSIONS[role]})
    expires = calendar.timegm(time.strptime(granted["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
    _cache[key] = (granted["token"], expires)
    return granted["token"]
