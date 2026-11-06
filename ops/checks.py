"""Deterministic exit gate for a node. Every check returns evidence, never an opinion."""
import hashlib
import re
import shutil
import tempfile
from pathlib import Path

from lib import CONTRACT_FILES, GREEN, HANDOFF_FIELDS, matches, parse_handoff, sh, tier_of

SECRET = re.compile(r"sk-or-[A-Za-z0-9-]{10,}|sb_secret_[A-Za-z0-9_-]{10,}|GOCSPX-[A-Za-z0-9_-]{10,}"
                    r"|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}|postgres(ql)?://[^<\s:]+:[^<\s@]+@")
TEST_FILE = ["*.test.ts", "*.spec.ts", "*.e2e.ts", "*_test.go"]


def fail(check, expected, saw):
    return {"check": check, "expected": expected, "saw": saw}


def changed_files(wt, base):
    out = sh(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=wt).stdout
    return [line for line in out.splitlines() if line]


def check_fence(files, block, cfg):
    fence = block.get("fence", [])
    outside = [f for f in files if not matches(f, fence) and not matches(f, cfg["always_allowed"])]
    return [fail("fence", f"changes only inside {fence}", f"outside: {outside}")] if outside else []


def check_contract(files, labels):
    touched = [f for f in files if f in CONTRACT_FILES]
    if touched and "contract-change" not in labels:
        return [fail("contract", "no contract edits without the contract-change label", str(touched))]
    return []


def check_secrets(wt, base, files):
    failures = []
    if any(Path(f).name == ".env" for f in files):
        failures.append(fail("secrets", "no .env committed", "a .env file is in the diff"))
    diff = sh(["git", "diff", "-U0", f"{base}...HEAD"], cwd=wt).stdout
    added = [l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    if any(SECRET.search(line) for line in added):
        failures.append(fail("secrets", "no credentials in added lines", "a credential pattern matched"))
    return failures


def check_handoff(text):
    if not text:
        return [fail("handoff", "a handoff file", "none written")]
    fields = parse_handoff(text)
    missing = [name for name in HANDOFF_FIELDS if name not in fields]
    if missing:
        return [fail("handoff", f"fields {HANDOFF_FIELDS}", f"missing: {missing}")]
    status = fields["STATUS"].split()[0]
    if status not in ("done", "gap", "needs_human"):
        return [fail("handoff", "STATUS done, gap or needs_human", status)]
    return []


def run_commands(commands, wt, name):
    for command in commands:
        done = sh(command, cwd=wt, check=False, timeout=1200)
        if done.returncode:
            tail = (done.stdout + done.stderr).strip()[-1500:]
            return [fail(name, f"`{command}` exits 0", tail)]
    return []


def check_red(wt, base, files, block, cfg):
    """The node's new tests must fail on the base commit, or they prove nothing."""
    code = [f for f in files if not matches(f, cfg["tier1"])]
    if not code or block.get("red") == "false":
        return []
    tests = [f for f in files if matches(f, TEST_FILE) and (Path(wt) / f).exists()]
    if not tests:
        return [fail("red", "code changes come with test changes", f"no tests changed for {code[:5]}")]
    commands = block.get("checks", [])
    if not commands:
        return [fail("red", "the node block lists checks", "no checks")]
    merge_base = sh(["git", "merge-base", base, "HEAD"], cwd=wt).stdout.strip()
    tmp = Path(tempfile.mkdtemp(prefix="red-")) / "tree"
    sh(["git", "worktree", "add", "--detach", "-q", str(tmp), merge_base], cwd=wt)
    try:
        for test in tests:
            target = tmp / test
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(wt) / test, target)
        if (Path(wt) / "node_modules").exists():
            (tmp / "node_modules").symlink_to(Path(wt).resolve() / "node_modules")
        if all(sh(c, cwd=tmp, check=False, timeout=600).returncode == 0 for c in commands):
            return [fail("red", "new tests fail on the base commit", "they pass without the change")]
        return []
    finally:
        sh(["git", "worktree", "remove", "--force", str(tmp)], cwd=wt, check=False)
        shutil.rmtree(tmp.parent, ignore_errors=True)


def gate(node, wt, cfg, handoff_text, base="origin/main", run_green=True):
    block = node.block or {}
    files = changed_files(wt, base)
    failures = check_handoff(handoff_text)
    if not files:
        failures.append(fail("diff", "at least one committed change", "empty diff against base"))
    failures += check_fence(files, block, cfg)
    failures += check_contract(files, node.labels)
    failures += check_secrets(wt, base, files)
    if run_green and not failures:
        failures += run_commands(GREEN[node.repo] + block.get("checks", []), wt, "green")
    if not failures:
        failures += check_red(wt, base, files, block, cfg)
    return {"tier": tier_of(files, cfg), "files": files, "failures": failures}


def saw_key(failures):
    text = "|".join(f"{f['check']}:{f['saw']}" for f in failures)
    return hashlib.sha256(re.sub(r"\d+(\.\d+)?m?s\b", "", text).encode()).hexdigest()[:16]


def feedback(failures, block, dead_ends):
    lines = []
    for item in failures:
        lines += [f"EXPECTED: {item['expected']}", f"SAW: {item['saw']}"]
    lines.append(f"FENCE: {block.get('fence', [])}")
    lines.append("DEAD ENDS: " + ("; ".join(dead_ends) if dead_ends else "none yet"))
    return "\n".join(lines)
