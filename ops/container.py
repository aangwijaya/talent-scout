"""The sandbox runtime (ADR 0002, 0003): agents and worktree code run in rootless Podman.

Fail closed: in container mode, a missing image or a failing podman raises SandboxError and nothing
runs on the host. The egress proxy (stage 3) and the agent clone (stage 4) are not here yet.
"""
import hashlib
import os
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from lib import OPS, ROOT, check_env

IMAGES = OPS / "images"
PODMAN = "podman"
HOME = "/home/agent"  # a tmpfs per run, so nothing a run writes there persists
# Host paths that mean nothing inside the image, and logins the sandbox gets another way.
DROP = {"PATH", "HOME", "GOPATH", "GOCACHE", "GOROOT", "GOTOOLCHAIN", "PNPM_HOME", "NVM_DIR", "COREPACK_HOME",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "TMPDIR", "SHELL",
        "CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY", "GH_CONFIG_DIR"}
CLAUDE_TOKEN = OPS / "env" / "claude.oauth-token"


class SandboxError(Exception):
    pass


def mode(cfg):
    value = cfg.get("sandbox", "host")
    if value not in ("host", "container"):
        raise SandboxError(f"sandbox must be host or container in ops/tiers.yaml, not {value!r}")
    return value


def image_tag(repo):
    digest = hashlib.sha256((IMAGES / "Containerfile").read_bytes() + repo.encode()).hexdigest()[:12]
    return f"localhost/talent-scout-{repo}:{digest}"


def require_image(repo):
    try:
        found = subprocess.run([PODMAN, "image", "exists", image_tag(repo)], capture_output=True).returncode == 0
    except OSError as err:
        raise SandboxError(f"podman is not usable: {err}") from err
    if not found:
        raise SandboxError(f"sandbox image {image_tag(repo)} is missing; run: ops/sandbox build {repo}")


def build(repo):
    subprocess.run([PODMAN, "build", "--target", repo, "-t", image_tag(repo), "-f", str(IMAGES / "Containerfile"),
                    str(IMAGES)], check=True)


def container_env(env):
    """The host env translated for the image: host paths and host logins dropped, caches in /tmp."""
    out = {k: v for k, v in env.items() if k not in DROP}
    out.update(HOME=HOME, GOPATH="/tmp/go", GOCACHE="/tmp/go-build", GH_CONFIG_DIR=f"{HOME}/.config/gh")
    if "GOPROXY" in out:
        out["GOPROXY"] = "https://proxy.golang.org"  # no `direct`: modules come through the proxy only
    for key, git_key in (("NAME", "user.name"), ("EMAIL", "user.email")):
        value = subprocess.run(["git", "config", git_key], capture_output=True, text=True).stdout.strip()
        if value:  # the host gitconfig is not mounted, so commits need the identity passed in
            out[f"GIT_AUTHOR_{key}"] = out[f"GIT_COMMITTER_{key}"] = value
    return out


def cache_mounts(repo):
    """Warm caches (ADR 0005), mounted as overlays: readable, and writes are discarded with the run."""
    mounts = []
    if repo == "server":
        modcache = subprocess.run(["go", "env", "GOMODCACHE"], capture_output=True, text=True).stdout.strip()
        if modcache and Path(modcache).is_dir():
            mounts.append((modcache, "O"))
    else:
        store = subprocess.run(["pnpm", "store", "path"], capture_output=True, text=True).stdout.strip()
        if store and Path(store).is_dir():
            mounts.append((store, "O"))
    return mounts


def git_common_dir(path):
    out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path,
                         capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else None


def podman_argv(repo, cfg, cwd, command, env_file, mounts=(), read_only=False, name=None):
    argv = [PODMAN, "run", "--rm", "-i", "--init", "--userns=keep-id",
            f"--cpus={cfg.get('sandbox_cpus', 4)}", f"--memory={cfg.get('sandbox_memory', '6g')}",
            f"--pids-limit={cfg.get('sandbox_pids', 1024)}", "--cap-drop=all", "--security-opt=no-new-privileges",
            "--tmpfs", f"{HOME}:rw,exec,mode=1777", "--tmpfs", "/tmp:rw,exec,mode=1777",
            "--env-file", str(env_file), "-w", str(cwd), "-v", f"{cwd}:{cwd}:{'ro' if read_only else 'rw'}"]
    if name:
        argv += ["--name", name]
    for source, opt in mounts:  # (path, option) mounts at the same path; (full spec, None) as given
        argv += ["-v", str(source) if opt is None else f"{source}:{source}:{opt}"]
    return argv + [image_tag(repo), *command]


@contextmanager
def env_file(env):
    """Values go to a 0600 file podman reads, never to argv where `ps` would show them."""
    fd, path = tempfile.mkstemp(prefix="sandbox-env-")
    try:
        with os.fdopen(fd, "w") as out:
            for key, value in env.items():
                if "\n" not in str(value):
                    out.write(f"{key}={value}\n")
        yield path
    finally:
        os.unlink(path)


def run_code(command, cwd, repo, cfg, timeout=None, extra_mounts=()):
    """Run a shell command from a worktree inside the sandbox, with the check env (no logins)."""
    require_image(repo)
    mounts = list(cache_mounts(repo)) + list(extra_mounts)
    common = git_common_dir(cwd)
    if common and not Path(common).is_relative_to(Path(cwd)):
        mounts.append((common, "ro"))
    name = f"ts-check-{uuid.uuid4().hex[:10]}"
    with env_file(container_env(check_env())) as path:
        argv = podman_argv(repo, cfg, cwd, ["sh", "-c", command], path, mounts=mounts, name=name)
        try:
            return subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run([PODMAN, "rm", "-f", "--ignore", name], capture_output=True)
            raise


def agent_mounts(wt, read_only, handoff_dir, opencode_home):
    mounts = [(str(handoff_dir), "rw")]
    common = git_common_dir(wt)
    if common:
        mounts.append((common, "ro" if read_only else "rw"))  # stage 4 replaces this with an agent clone
    config = Path.home() / ".config" / "opencode"
    if config.is_dir():
        mounts.append((f"{config}:{HOME}/.config/opencode:O", None))
    mounts.append((f"{opencode_home}:{HOME}/.local/share/opencode:rw", None))
    return mounts


@contextmanager
def agent_home():
    """A throwaway copy of OpenCode's login; Claude gets its long-lived token through the env file."""
    with tempfile.TemporaryDirectory(prefix="sandbox-opencode-") as tmp:
        auth = Path.home() / ".local" / "share" / "opencode" / "auth.json"
        if auth.exists():
            target = Path(tmp, "auth.json")
            target.write_bytes(auth.read_bytes())
            target.chmod(0o600)
        yield tmp


def claude_token():
    if not CLAUDE_TOKEN.exists():
        raise SandboxError(f"no Claude token at {CLAUDE_TOKEN}; create it with `claude setup-token` (WORKFLOW.md)")
    return CLAUDE_TOKEN.read_text().strip()


def probe(repo, cfg):
    """Run images/probe.sh in the sandbox and return its PASS/FAIL/PENDING lines."""
    require_image(repo)
    allowed = sorted(set(container_env(check_env())) | {"PWD", "SHLVL", "HOSTNAME", "container", "_", "TERM",
                                                         "PATH", "LANG", "COREPACK_ENABLE_AUTO_PIN",
                                                         "COREPACK_HOME", "NODE_VERSION", "YARN_VERSION",
                                                         "GOTOOLCHAIN", "GOPROXY", "GOFLAGS",
                                                         "PLAYWRIGHT_BROWSERS_PATH", "OLDPWD"})
    memory = str(parse_bytes(cfg.get("sandbox_memory", "6g")))
    with tempfile.TemporaryDirectory() as cwd, env_file(container_env(check_env())) as path:
        argv = podman_argv(repo, cfg, cwd, ["sh", "/opt/probe/probe.sh", str(Path.home()), str(ROOT), memory,
                                            str(cfg.get("sandbox_pids", 1024)), ",".join(allowed)], path,
                           mounts=[(f"{IMAGES / 'probe.sh'}:/opt/probe/probe.sh:ro", None)])
        done = subprocess.run(argv, text=True, capture_output=True, timeout=120)
    if done.returncode:
        raise SandboxError(f"probe could not run in the {repo} sandbox: {done.stderr.strip()[-500:]}")
    return [line for line in done.stdout.splitlines() if line.strip()]


def parse_bytes(text):
    units = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
    text = str(text).strip().lower()
    return int(float(text[:-1]) * units[text[-1]]) if text[-1] in units else int(text)
