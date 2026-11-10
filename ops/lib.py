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
STOP_FILE = OPS / "STOP"
REPOS = {"client": "aangwijaya/talent-scout-client", "server": "aangwijaya/talent-scout-server"}
AGENTS = {"client": "claude", "server": "opencode"}
GREEN = {
    "client": ["pnpm check", "pnpm lint", "pnpm test", "pnpm build"],
    "server": ['test -z "$(gofmt -l .)"', "go build ./...", "go vet ./...", "go test ./..."],
}
HANDOFF_FIELDS = ["STATUS", "GOAL", "TRIED", "KNOWN", "NEXT", "NEVER"]
HANDOFF_MARK = "<!-- handoff -->"
CONTRACT_FILES = ["docs/API.md", "docs/EVENTS.md", "docs/PRD.md"]


def sh(cmd, cwd=None, timeout=None, check=True):
    done = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), text=True,
                          capture_output=True, timeout=timeout)
    if check and done.returncode:
        raise RuntimeError(f"{cmd}: {done.stderr.strip()[-500:]}")
    return done


def gh(*args):
    return sh(["gh", *args]).stdout


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
    for name in HANDOFF_FIELDS + ["GAPS"]:
        match = re.search(rf"^{name}:[ \t]*(.*?)(?=^(?:{'|'.join(HANDOFF_FIELDS + ['GAPS'])}):|\Z)",
                          text, re.S | re.M)
        if match and match.group(1).strip():
            found[name] = match.group(1).strip()
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
