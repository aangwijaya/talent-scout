"""Spending guards for unattended runs: the daily budget, the quota pause and one node per repo.

- Daily budget: the agents' reported cost of every run today, summed from the audit ledger, stays under
  `daily_budget_usd`. The day starts at 00:00 in `budget_tz`. Each attempt and review may spend only what
  is left (and never more than `budget_usd`), so the cap holds within a run too, not just between runs.
- Quota pause: when an agent's plan says "you've hit your limit", nodes for that agent wait until the
  reset it names (or `quota_pause_min`). The interrupted attempt is not counted against the node.
- One node per repo: a lock per repo, held while a node runs, so two runners never work one repo.

Every pause and resume is in the ledger, so `ops/report` can show why nothing ran.
"""
import fcntl
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import ledger
from lib import STATE

PAUSE_FILE = STATE / "paused.json"
MIN_ATTEMPT_USD = 1  # below this an attempt can't do useful work; wait for tomorrow instead
SLACK = timedelta(minutes=2)  # resume a little after the reset the provider names, not on the second
LONGEST_PAUSE = timedelta(days=8)  # a weekly limit is the longest one plans have
# How plans and APIs say the quota is gone. A balance that ran out (402) is not here: it needs the human.
QUOTA = re.compile(r"hit your (?:usage |weekly |session )?limit|usage limit (?:reached|exceeded)|"
                   r"rate[ _-]?limit|quota (?:exceeded|exhausted)|too many requests|\b429\b", re.I)


class BudgetSpent(Exception):
    pass


class Busy(Exception):
    pass


def now_utc():
    return datetime.now(timezone.utc)


def tz(cfg):
    return ZoneInfo(cfg.get("budget_tz", "Asia/Jakarta"))


def day_start(cfg, now=None):
    local = (now or now_utc()).astimezone(tz(cfg))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def next_reset(cfg, now=None):
    return day_start(cfg, now) + timedelta(days=1)  # aware arithmetic is wall-clock: the next local midnight


def spent_today(cfg, now=None, directory=None):
    """Reported USD of every agent run since 00:00 in budget_tz, from the ledger."""
    since = day_start(cfg, now)
    total = 0.0
    for _, _, record in ledger.records(directory):
        cost = record.get("cost_usd")
        if cost and datetime.fromisoformat(record["ts"]) >= since:
            total += cost
    return round(total, 4)


def allowance(cfg, now=None):
    """What the next agent run may spend: the per-run budget, or what is left of today's, whichever is less.
    Raises BudgetSpent when what is left is too little to be worth a run."""
    left = cfg["daily_budget_usd"] - spent_today(cfg, now)
    if left < MIN_ATTEMPT_USD:
        raise BudgetSpent(f"daily budget spent (${cfg['daily_budget_usd'] - left:.2f} of "
                          f"${cfg['daily_budget_usd']}); resumes {next_reset(cfg, now):%Y-%m-%d %H:%M %Z}")
    return round(min(cfg["budget_usd"], left), 2)


def quota_hit(text):
    return bool(text and QUOTA.search(text))


def quota_reset(text, cfg, now=None):
    """When the provider says the quota comes back, if the message names it."""
    now = now or now_utc()
    epoch = re.search(r"\|(\d{10})\b", text)
    if epoch:
        return datetime.fromtimestamp(int(epoch.group(1)), timezone.utc)
    after = re.search(r"(?:try again|retry) (?:in|after) (\d+)\s*(second|minute|hour)s?", text, re.I)
    if after:
        return now + timedelta(**{after.group(2).lower() + "s": int(after.group(1))})
    at = re.search(r"resets? (?:at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([\w/+-]+)\))?", text, re.I)
    if not at:
        return None
    hour, minute, half, zone = int(at.group(1)), int(at.group(2) or 0), (at.group(3) or "").lower(), at.group(4)
    if half:
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    try:
        where = ZoneInfo(zone) if zone else tz(cfg)
    except (KeyError, ValueError):
        where = tz(cfg)
    local = now.astimezone(where)
    reset = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return reset if reset > local else reset + timedelta(days=1)


def _pauses():
    try:
        return json.loads(PAUSE_FILE.read_text()) if PAUSE_FILE.exists() else {}
    except ValueError:
        return {"unknown": {"kind": "unknown", "reason": "unreadable pause file", "until": None}}


def pause(agent, reason, until, kind):
    """Stop starting work for this agent until `until`. The node that hit it is handed back, not failed.
    Pauses are per agent: Claude's plan running out says nothing about OpenCode's."""
    STATE.mkdir(parents=True, exist_ok=True)
    pauses = _pauses()
    pauses[agent] = {"kind": kind, "reason": reason[:300], "until": until.isoformat()}
    PAUSE_FILE.write_text(json.dumps(pauses))
    ledger.append("queue.paused", actor="orchestrator", agent=agent, kind=kind, reason=reason[:300],
                  until=until.isoformat(timespec="seconds"))


def pause_for_quota(agent, error, cfg, now=None):
    now = now or now_utc()
    reset = quota_reset(error, cfg, now)
    until = reset + SLACK if reset and now < reset < now + LONGEST_PAUSE else \
        now + timedelta(minutes=cfg.get("quota_pause_min", 60))
    pause(agent, error, until, "quota")
    return until


def paused(now=None):
    """The pauses still in force, by agent. One whose time has come ends here, and the ledger says so.
    An unreadable pause file holds every agent until the human clears it."""
    now = now or now_utc()
    active = {}
    for agent, info in _pauses().items():
        try:
            until = datetime.fromisoformat(info["until"])
        except (TypeError, ValueError, KeyError):
            active[agent] = info
            continue
        if now >= until:
            resume("time", agent)
        else:
            active[agent] = info
    return active


def resume(by, agent=None):
    """End one agent's pause, or every pause (the human's `ops/stop --clear`)."""
    pauses = _pauses()
    ended = [a for a in pauses if agent in (None, a)]
    for name in ended:
        del pauses[name]
        ledger.append("queue.resumed", actor="human" if by == "human" else "orchestrator", agent=name, by=by)
    if not pauses:
        PAUSE_FILE.unlink(missing_ok=True)
    elif ended:
        PAUSE_FILE.write_text(json.dumps(pauses))


@contextmanager
def repo_lock(repo):
    """Held while a node of this repo runs. The kernel drops it if the runner dies, so it never goes stale."""
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / f"{repo}.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy(f"another node is already running in {repo}")
        yield
