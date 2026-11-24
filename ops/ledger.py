"""Audit ledger (GLOSSARY): the append-only, tamper-evident record of what the orchestrator did and decided.

One JSON object per line, in monthly files: ~/.local/state/talent-scout-ops/audit-YYYY-MM.jsonl. The files
live outside the repo and outside every sandbox mount, and are never deleted.

Each record carries the hash of the record before it, and its own hash over everything else. Changing,
dropping or reordering a line breaks the chain from that line on, and `ops/report --verify` says where.
Dropping lines from the end can't break a chain, so the head (seq and hash) is also written into every PR
body and handoff comment: GitHub's timestamps witness it, and --verify checks the ledger still holds
every head it ever published.
"""
import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64
ANCHOR = re.compile(r"audit-ledger-head: seq (\d+) sha256 ([0-9a-f]{64})")


def ledger_dir():
    if os.environ.get("OPS_LEDGER_DIR"):
        return Path(os.environ["OPS_LEDGER_DIR"])
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "talent-scout-ops"


def files(directory=None):
    return sorted((directory or ledger_dir()).glob("audit-*.jsonl"))


def digest(record):
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode()).hexdigest()


def _tail(directory):
    for path in reversed(files(directory)):
        lines = path.read_text().splitlines()
        if lines:
            last = json.loads(lines[-1])
            return last["seq"], last["hash"]
    return 0, GENESIS


def append(event, **fields):
    """Add one record and return it. A lock keeps parallel runners (one per repo) on one chain."""
    directory = ledger_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(directory / ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        seq, prev = _tail(directory)
        now = datetime.now(timezone.utc)
        record = {"seq": seq + 1, "ts": now.isoformat(timespec="seconds"), "event": event,
                  **{k: v for k, v in fields.items() if v is not None}, "prev": prev}
        record["hash"] = digest(record)
        with open(directory / f"audit-{now:%Y-%m}.jsonl", "a") as out:
            out.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            out.flush()
            os.fsync(out.fileno())
    return record


def records(directory=None):
    for path in files(directory):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            yield path.name, number, json.loads(line)


def verify(directory=None, anchors=()):
    """Check the whole chain, then that every published head (seq, hash) is still in it.
    Returns (problems, count, head)."""
    problems, prev, expected, by_seq = [], GENESIS, 1, {}
    try:
        for name, number, record in records(directory):
            where = f"{name}:{number}"
            if record.get("seq") != expected:
                problems.append(f"{where}: seq {record.get('seq')}, expected {expected} (a record is missing or moved)")
            if record.get("prev") != prev:
                problems.append(f"{where}: does not follow the record before it")
            if digest(record) != record.get("hash"):
                problems.append(f"{where}: content changed after it was written")
            by_seq[record.get("seq")] = record.get("hash")
            prev, expected = record.get("hash"), (record.get("seq") or expected) + 1
    except ValueError as err:
        problems.append(f"unreadable line: {err}")
    for seq, value in anchors:
        if by_seq.get(seq) != value:
            problems.append(f"published head seq {seq} is {'missing' if seq not in by_seq else 'different'} "
                            "in the local ledger")
    return problems, expected - 1, prev


def head(directory=None):
    return _tail(directory or ledger_dir())


def anchor():
    """The head as a hidden line for a PR body or comment, where GitHub's timestamp witnesses it."""
    seq, value = head()
    return f"<!-- audit-ledger-head: seq {seq} sha256 {value} -->"


def anchors_in(texts):
    return sorted({(int(seq), value) for text in texts for seq, value in ANCHOR.findall(text or "")})
