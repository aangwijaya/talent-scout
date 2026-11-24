"""Shared helpers for the Talent Scout control plane. Python stdlib + the gh CLI."""
import fnmatch
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPS = ROOT / "ops"
CACHE = OPS / ".cache"
STATE = OPS / ".state"
HANDOFF_DIR = STATE / "handoff"
STOP_FILE = OPS / "STOP"  # ops/stop --now: running nodes stop at once and nothing starts
HOLD_FILE = STATE / "hold"  # ops/stop: nothing new starts; a running node finishes
REPOS = {"client": "aangwijaya/talent-scout-client", "server": "aangwijaya/talent-scout-server"}
AGENTS = {"client": "claude", "server": "opencode"}
GREEN = {
    "client": ["pnpm check", "pnpm lint", "pnpm test", "pnpm build"],
    "server": ['test -z "$(gofmt -l .)"', "go build ./...", "go vet ./...", "go test ./..."],
}
HANDOFF_FIELDS = ["STATUS", "GOAL", "TRIED", "KNOWN", "NEXT", "NEVER"]
HANDOFF_MARK = "<!-- handoff -->"
CONTRACT_FILES = ["docs/API.md", "docs/EVENTS.md", "docs/PRD.md"]


# Code from a worktree (agents, green and red checks, dependency installs) only ever sees these
# variables. Anything not listed is dropped: database URLs, API keys, the human's tokens and sockets.
TOOLCHAIN_ENV_KEEP = {
    "PATH": "finds go, pnpm, node, git, gh and the agent CLIs",
    "HOME": "the agent CLIs keep their own login and config under it",
    "USER": "some tools look up the current user",
    "LOGNAME": "same as USER",
    "SHELL": "OpenCode's bash tool starts this shell",
    "LANG": "text encoding",
    "LC_ALL": "text encoding, when set",
    "TERM": "terminal type for CLI output",
    "TZ": "time zone, when set",
    "TMPDIR": "temp files, when set",
    "XDG_CONFIG_HOME": "OpenCode config, when moved out of HOME",
    "XDG_DATA_HOME": "OpenCode login, when moved out of HOME",
    "XDG_CACHE_HOME": "tool caches, when moved out of HOME",
    "XDG_STATE_HOME": "tool state, when moved out of HOME",
    "GOPATH": "Go workspace",
    "GOMODCACHE": "Go module cache, which agents may read",
    "GOCACHE": "Go build cache",
    "GOROOT": "Go install, when not on the default path",
    "GOFLAGS": "Go flags, when set",
    "GOTOOLCHAIN": "Go toolchain choice",
    "GOPROXY": "Go module proxy",
    "PNPM_HOME": "pnpm install and store",
    "COREPACK_HOME": "corepack, which provides pnpm",
    "COREPACK_ENABLE_AUTO_PIN": "keeps corepack from editing package.json",
    "NVM_DIR": "node from nvm, when used",
}
# Only the agent CLIs need these; checks and installs never do.
AGENT_LOGIN_KEEP = {
    "CLAUDE_CONFIG_DIR": "Claude CLI login, when moved out of HOME",
    "ANTHROPIC_API_KEY": "Claude CLI login, when it uses an API key instead of a stored login",
    "CLAUDE_CODE_OAUTH_TOKEN": "Claude CLI login, when it uses a long-lived token",
}
PROXY_ENV_KEEP = {name: "egress proxy (sandbox stage 3)" for name in
                  ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")}
CHECK_ENV_KEEP = {**TOOLCHAIN_ENV_KEEP, **PROXY_ENV_KEEP}


def check_env():
    """The environment for commands that run code from a worktree: toolchain only, no logins or secrets."""
    return {k: os.environ[k] for k in CHECK_ENV_KEEP if k in os.environ}


def sh(cmd, cwd=None, timeout=None, check=True, env=None):
    done = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), text=True,
                          capture_output=True, timeout=timeout, env=env)
    if check and done.returncode:
        raise RuntimeError(f"{cmd}: {done.stderr.strip()[-500:]}")
    return done


def run_code(command, cwd, repo, cfg, timeout=None, extra_mounts=()):
    """Run a shell command that executes code from a worktree. Always in the sandbox (ADR 0002)."""
    import container
    container.mode(cfg)
    return container.run_code(command, cwd, repo, cfg, timeout=timeout, extra_mounts=extra_mounts)

def gh_env(cfg=None):
    """The environment for the orchestrator's own gh and git calls: the human's login (legacy), or a
    short-lived ts-orchestrator token for both repos (ADR 0006)."""
    import identity
    cfg = cfg or config()
    if identity.mode(cfg) == "legacy":
        return None
    return {**os.environ, "GH_TOKEN": identity.token("orchestrator", list(REPOS.values()), cfg),
            "GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"}


def gh(*args):
    return sh(["gh", *args], env=gh_env()).stdout


def push_argv(full, branch, cfg=None):
    """git push for a node branch. Legacy: the origin remote and the human's SSH key. Apps: HTTPS with the
    orchestrator token, which gh hands to git as a credential helper; the token is never in argv."""
    import identity
    if identity.mode(cfg or config()) == "legacy":
        return ["git", "push", "-q", "-u", "origin", "HEAD"]
    return ["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
            "push", "-q", f"https://github.com/{full}.git", f"HEAD:refs/heads/{branch}"]


def flat_yaml(text):
    """Parse `key: value` and `key: [a, b]` lines, the only YAML this project uses."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        if value.startswith("[") and value.endswith("]"):
            out[key] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        elif value.lstrip("-").isdigit():
            out[key] = int(value)
        else:
            out[key] = value
    return out


def config():
    return flat_yaml((OPS / "tiers.yaml").read_text())


def parse_node_block(body):
    match = re.search(r"```yaml\n((?:fence|blocked_by|checks|red):.*?)```", body or "", re.S)
    return flat_yaml(match.group(1)) if match else None


@dataclass
class Node:
    id: str
    repo: str
    number: int
    title: str
    body: str
    closed: bool
    labels: list
    block: dict | None
    comments: list = field(default_factory=list)

    def has(self, label):
        return label in self.labels

    @property
    def agent(self):
        for label in self.labels:
            if label.startswith("agent:"):
                return label.split(":", 1)[1]
        return None

    @property
    def blocked_by(self):
        return (self.block or {}).get("blocked_by", [])


def to_node(repo, raw):
    return Node(id=f"{repo}#{raw['number']}", repo=repo, number=raw["number"], title=raw["title"],
                body=raw["body"] or "", closed=raw["state"] == "CLOSED",
                labels=[l["name"] for l in raw["labels"]], block=parse_node_block(raw["body"]),
                comments=[c["body"] for c in raw.get("comments", [])])


def load_nodes(ttl=60):
    """One list call per repo, cached, so polling can't burn the shared API limit."""
    CACHE.mkdir(parents=True, exist_ok=True)
    nodes = {}
    for repo, full in REPOS.items():
        path = CACHE / f"{repo}.json"
        if not path.exists() or time.time() - path.stat().st_mtime > ttl:
            # Write then rename, so a runner for the other repo never reads half a file.
            fresh = path.with_suffix(f".{os.getpid()}.tmp")
            fresh.write_text(gh("issue", "list", "-R", full, "--state", "all", "--limit", "200",
                                "--json", "number,title,body,state,labels,comments"))
            fresh.replace(path)
        for raw in json.loads(path.read_text()):
            node = to_node(repo, raw)
            nodes[node.id] = node
    return nodes


def analyze(nodes):
    """Return ready, waiting, active and human lists, ranked by what each node unblocks."""
    open_nodes = {k: n for k, n in nodes.items() if not n.closed}
    dependents = {k: set() for k in open_nodes}
    for key, node in open_nodes.items():
        for dep in node.blocked_by:
            if dep in dependents:
                dependents[dep].add(key)

    def reach(key, seen):
        for child in dependents[key]:
            if child not in seen:
                seen.add(child)
                reach(child, seen)
        return seen

    weight = {k: len(reach(k, set())) for k in open_nodes}
    cycles = [k for k in open_nodes if k in reach(k, set())]
    result = {"ready": [], "waiting": [], "active": [], "human": [], "unmapped": [], "cycles": cycles}
    for key, node in open_nodes.items():
        missing = [d for d in node.blocked_by if d not in nodes or not nodes[d].closed]
        if node.block is None:
            result["unmapped"].append(key)
        elif node.has("needs:human") or node.has("status:blocked") or node.agent == "human":
            result["human"].append(key)
        elif node.has("status:in-progress") or node.has("status:review"):
            result["active"].append(key)
        elif missing or key in cycles:
            result["waiting"].append({"id": key, "on": missing})
        else:
            result["ready"].append(key)
    result["ready"].sort(key=lambda k: (not nodes[k].has("type:gap"), -weight[k], nodes[k].number))
    return result


def matches(path, globs):
    """`*` and `?` are wildcards; `[` is literal, because SvelteKit route params live in brackets."""
    return any(fnmatch.fnmatch(path, glob.replace("[", "[[]")) for glob in globs)


def tier_of(files, cfg):
    if any(matches(f, cfg["tier3"]) for f in files):
        return 3
    return 1 if files and all(matches(f, cfg["tier1"]) for f in files) else 2


def parse_handoff(text):
    """Split a handoff into its fields. Missing fields stay absent."""
    found = {}
    names = HANDOFF_FIELDS + ["GAPS", "EGRESS"]
    for name in names:
        match = re.search(rf"^{name}:[ \t]*(.*?)(?=^(?:{'|'.join(names)}):|\Z)",
                          text, re.S | re.M)
        if match and match.group(1).strip():
            found[name] = match.group(1).strip()
    return found


def with_egress(text, hosts):
    """Add the hosts the sandbox's proxy refused as their own handoff field (ADR 0002)."""
    if not hosts:
        return text
    return (text.rstrip() + f"\nEGRESS: refused {', '.join(hosts)}. If the need is legitimate, add the host to "
            "ops/egress-allowlist in a reviewed PR; otherwise treat it as suspicious.")


def egress_refusals(state_dir=STATE):
    """node id -> refused hosts, from the orchestrator's per-node state files."""
    found = {}
    for path in sorted(Path(state_dir).glob("*.json")):
        try:
            hosts = json.loads(path.read_text()).get("egress_refused")
        except (ValueError, AttributeError):
            continue
        if hosts:
            repo, _, number = path.stem.partition("-")
            found[f"{repo}#{number}"] = hosts
    return found


def last_handoff(node):
    for comment in reversed(node.comments):
        if comment.startswith(HANDOFF_MARK):
            return comment
    return None


def state_path(node_id):
    STATE.mkdir(parents=True, exist_ok=True)
    return STATE / (node_id.replace("#", "-") + ".json")


def handoff_path(node_id):
    """The one file outside the worktree an agent may write."""
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    return HANDOFF_DIR / (node_id.replace("#", "-") + ".md")


def load_state(node_id):
    path = state_path(node_id)
    return json.loads(path.read_text()) if path.exists() else {"attempts": 0, "saws": [], "dead_ends": []}


def save_state(node_id, state):
    state_path(node_id).write_text(json.dumps(state, indent=1))
