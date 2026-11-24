"""Which tiers merge by themselves, and into where (ADR 0008).

- A node merges by itself only into the integration branch (`dev`), never into the release branch
  (`main`): production changes only through a release PR the human merges. While the app repos have no
  `dev` yet (integration_branch: main), nothing merges by itself.
- Tier 3 never merges by itself, whatever `automerge` says: contracts, auth, migrations, CI and the
  agents' own rules spread to other repos and later nodes.
- It also needs the verifier's `merge` verdict, on top of green CI and a review with no High.
- A High finding that got past the review ("escaped") turns tier 2 auto-merge off until the human turns it
  back on (`ops/report --automerge on`). The verifier reports it, or the human does with
  `ops/report --note NODE escaped-high`. Both land in the ledger.
"""
import json

import ledger
from lib import RELEASE_BRANCH, STATE, integration

HOLD_FILE = STATE / "automerge-off.json"
ALWAYS_HUMAN = 3


def held():
    """Tiers the human must merge for now, after an escaped High: {tier: reason}."""
    try:
        return {int(k): v for k, v in json.loads(HOLD_FILE.read_text()).items()} if HOLD_FILE.exists() else {}
    except ValueError:
        return {2: "unreadable automerge-off.json"}


def tiers(cfg):
    """The tiers that may merge by themselves right now."""
    if integration(cfg) == RELEASE_BRANCH:
        return []
    off = held()
    return [int(t) for t in cfg.get("automerge", []) if int(t) != ALWAYS_HUMAN and int(t) not in off]


def why_not(tier, cfg):
    """Why a node of this tier waits for the human, or None if it may merge by itself."""
    if tier in tiers(cfg):
        return None
    if integration(cfg) == RELEASE_BRANCH:
        return f"nodes merge into {RELEASE_BRANCH} only by the human until the integration branch exists"
    if tier == ALWAYS_HUMAN:
        return "tier 3 always needs the human"
    if tier in held():
        return f"tier {tier} auto-merge is off: {held()[tier]}"
    return f"tier {tier} is not in automerge"


def escaped_high(node_id, text, by):
    """Record a High that got past the review, and turn tier 2 auto-merge off."""
    reason = f"escaped High in {node_id}: {text[:200]}"
    off = held()
    if 2 not in off:
        off[2] = reason
        STATE.mkdir(parents=True, exist_ok=True)
        HOLD_FILE.write_text(json.dumps(off))
        ledger.append("automerge.off", actor=by, node=node_id, tier=2, reason=reason)
    return reason


def turn_on(by="human"):
    if HOLD_FILE.exists():
        HOLD_FILE.unlink()
        ledger.append("automerge.on", actor=by, tier=2)
        return True
    return False
