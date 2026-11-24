"""Spending guards (ops/budget.py) and how ops/run uses them: the daily budget, quota pauses, one node per repo."""
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OPS))
import budget
import container
import fakes
import ledger
import lib
from fakes import git_worktree

CFG = {"budget_usd": 6, "daily_budget_usd": 20, "budget_tz": "Asia/Jakarta", "quota_pause_min": 60}
UTC = timezone.utc
LIMIT = "You've hit your limit · resets 3pm (Asia/Jakarta)"


def setUpModule():
    fakes.sandbox_for_tests(tempfile.mkdtemp(prefix="ops-sandbox-"))


def load_run():
    loader = importlib.machinery.SourceFileLoader("run", str(OPS / "run"))
    spec = importlib.util.spec_from_loader("run", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    token = Path(tempfile.mkdtemp(prefix="ops-token-"), "agent.gh-token")
    token.write_text("github_pat_" + "x" * 40 + "\n")
    module.AGENT_GH_TOKEN = token  # never the real one
    return module


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


class BudgetCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        saved = os.environ.get("OPS_LEDGER_DIR")
        os.environ["OPS_LEDGER_DIR"] = str(self.dir / "ledger")
        self.addCleanup(lambda: os.environ.__setitem__("OPS_LEDGER_DIR", saved) if saved
                        else os.environ.pop("OPS_LEDGER_DIR", None))
        old = budget.STATE, budget.PAUSE_FILE
        budget.STATE, budget.PAUSE_FILE = self.dir / "state", self.dir / "state" / "paused.json"
        self.addCleanup(lambda: setattr(budget, "STATE", old[0]) or setattr(budget, "PAUSE_FILE", old[1]))

    def spend(self, cost, ts=None, event="attempt"):
        record = ledger.append(event, actor="orchestrator", node="client#1", cost_usd=cost)
        if ts:
            # Rewrite the time the way the record would have been written then, keeping the chain valid.
            [path] = ledger.files()
            lines = path.read_text().splitlines()
            record["ts"] = ts
            record["hash"] = ledger.digest(record)
            lines[-1] = json.dumps(record, sort_keys=True)
            path.write_text("\n".join(lines) + "\n")
        return record

    def events(self, name):
        return [r for _, _, r in ledger.records() if r["event"] == name]


class DailyBudget(BudgetCase):
    def test_the_day_starts_at_midnight_jakarta(self):
        self.spend(5, "2026-11-24T16:59:00+00:00")  # 23:59 WIB on the 24th: yesterday
        self.spend(4, "2026-11-24T17:00:00+00:00")  # 00:00 WIB on the 25th: today
        self.spend(3, "2026-11-25T09:00:00+00:00")
        now = at("2026-11-25T10:00:00")
        self.assertEqual(budget.spent_today(CFG, now), 7)
        self.assertEqual(budget.next_reset(CFG, now).astimezone(UTC), at("2026-11-25T17:00:00"))

    def test_reviews_count_too_and_records_without_cost_do_not(self):
        self.spend(2, event="review.run")
        self.spend(None, event="gate")
        self.assertEqual(budget.spent_today(CFG), 2)

    def test_a_run_may_spend_only_what_is_left_of_the_day(self):
        self.assertEqual(budget.allowance(CFG), 6)
        self.spend(17)
        self.assertEqual(budget.allowance(CFG), 3)
        self.spend(2.5)
        with self.assertRaisesRegex(budget.BudgetSpent, r"daily budget spent \(\$19.50 of \$20\)"):
            budget.allowance(CFG)


class Quota(BudgetCase):
    def test_limit_messages_are_recognized_and_a_balance_is_not(self):
        for text in (LIMIT, "Claude AI usage limit reached|1764061200", "opencode APIError 429: Too Many Requests",
                     "rate_limit_error", "quota exceeded for this plan"):
            self.assertTrue(budget.quota_hit(text), text)
        for text in (None, "", "opencode APIError 402: Insufficient Balance", "claude error_max_budget_usd: "):
            self.assertFalse(budget.quota_hit(text), text)

    def test_the_reset_time_is_read_from_the_message(self):
        now = at("2026-11-25T03:00:00")  # 10:00 WIB
        self.assertEqual(budget.quota_reset(LIMIT, CFG, now), at("2026-11-25T08:00:00"))
        self.assertEqual(budget.quota_reset("resets 9am (Asia/Jakarta)", CFG, now), at("2026-11-26T02:00:00"))
        self.assertEqual(budget.quota_reset("resets 5:30pm", CFG, now), at("2026-11-25T10:30:00"))
        self.assertEqual(budget.quota_reset("usage limit reached|1764061200", CFG, now),
                         datetime.fromtimestamp(1764061200, UTC))
        self.assertEqual(budget.quota_reset("429: try again in 20 minutes", CFG, now), now + timedelta(minutes=20))
        self.assertIsNone(budget.quota_reset("rate limited", CFG, now))

    def test_a_pause_holds_one_agent_until_its_time_then_ends_in_the_ledger(self):
        now = at("2026-11-25T03:00:00")
        until = budget.pause_for_quota("claude", LIMIT, CFG, now)
        self.assertEqual(until, at("2026-11-25T08:02:00"))
        self.assertEqual(set(budget.paused(now)), {"claude"})
        self.assertEqual(budget.paused(at("2026-11-25T08:03:00")), {})
        self.assertFalse(budget.PAUSE_FILE.exists())
        self.assertEqual([(r["agent"], r["by"]) for r in self.events("queue.resumed")], [("claude", "time")])

    def test_an_unnamed_reset_waits_quota_pause_min_and_the_human_can_end_it(self):
        now = at("2026-11-25T03:00:00")
        self.assertEqual(budget.pause_for_quota("opencode", "429", CFG, now), now + timedelta(minutes=60))
        budget.pause_for_quota("claude", LIMIT, CFG, now)
        budget.resume("human")
        self.assertEqual(budget.paused(now), {})
        self.assertEqual({r["actor"] for r in self.events("queue.resumed")}, {"human"})

    def test_an_unreadable_pause_file_holds_everything(self):
        budget.STATE.mkdir(parents=True)
        budget.PAUSE_FILE.write_text("{not json")
        self.assertIn("unknown", budget.paused())

    def test_ops_stop_clear_ends_a_pause(self):
        budget.pause_for_quota("claude", LIMIT, CFG)
        env = {**os.environ, "PYTHONPATH": str(OPS)}
        code = ("import sys, runpy; sys.argv=['stop','--clear']; import budget, pathlib, lib;"
                f"budget.STATE=pathlib.Path({str(budget.STATE)!r}); budget.PAUSE_FILE=pathlib.Path({str(budget.PAUSE_FILE)!r});"
                f"lib.STOP_FILE=pathlib.Path({str(self.dir / 'STOP')!r});"
                f"runpy.run_path({str(OPS / 'stop')!r}, run_name='__main__')")
        subprocess.run([sys.executable, "-c", code], env=env, check=True, capture_output=True)
        self.assertEqual(budget.paused(), {})


class RepoLock(BudgetCase):
    def test_a_second_node_in_the_same_repo_is_refused_and_another_repo_is_not(self):
        with budget.repo_lock("server"):
            with self.assertRaisesRegex(budget.Busy, "already running in server"):
                with budget.repo_lock("server"):
                    pass
            with budget.repo_lock("client"):
                pass
        with budget.repo_lock("server"):
            pass  # released when the first runner finished

    def test_the_lock_is_released_when_the_runner_dies(self):
        code = (f"import sys; sys.path.insert(0, {str(OPS)!r}); import budget, pathlib, os;"
                f"budget.STATE=pathlib.Path({str(budget.STATE)!r});"
                "cm=budget.repo_lock('server'); cm.__enter__(); print('held', flush=True); os._exit(0)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "held")
        with budget.repo_lock("server"):
            pass


class ClaudeCost(BudgetCase):
    def fake_claude(self, tmp, output, code=0):
        fake = Path(tmp, "claude")
        fake.write_text(f"#!/bin/sh\ncat > /dev/null\ncat <<'EOF'\n{output}\nEOF\nexit {code}\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    def run_claude(self, output, code=0, cfg=None):
        run = load_run()
        tmp = self.dir / "bin"
        tmp.mkdir(exist_ok=True)
        self.fake_claude(tmp, output, code)
        run.STATE, run.HANDOFF_DIR = self.dir, self.dir / "handoff"
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{tmp}:{old_path}"
        try:
            return run, run.run_agent("claude", "p", git_worktree(self.dir / "repos"),
                                      cfg or {"timeout_min": 1, "budget_usd": 3, "claude_model": "c",
                                              "claude_effort": "low"}, read_only=True)
        finally:
            os.environ["PATH"] = old_path

    def test_claude_reports_its_cost_and_reply_as_json(self):
        result = {"type": "result", "subtype": "success", "is_error": False, "result": "all done",
                  "total_cost_usd": 1.2345}
        run, (code, text) = self.run_claude(json.dumps(result))
        self.assertEqual((code, text, run.LAST_COST, run.LAST_AGENT_ERROR), (0, "all done", 1.2345, None))
        last_run = [l for l in Path(container.PODMAN).with_name("podman.log").read_text().splitlines()
                    if l.startswith("run ") and " claude -p " in l][-1]
        self.assertIn("--output-format json", last_run)

    def test_a_spent_run_budget_is_an_error_with_its_cost(self):
        result = {"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "total_cost_usd": 3.01}
        run, (code, _) = self.run_claude(json.dumps(result), code=1)
        self.assertEqual(run.LAST_COST, 3.01)
        self.assertTrue(run.LAST_AGENT_ERROR.startswith("claude error_max_budget_usd"))

    def test_a_limit_message_raises_quota_hit(self):
        result = {"type": "result", "subtype": "success", "is_error": True, "result": LIMIT, "total_cost_usd": 0}
        with self.assertRaisesRegex(Exception, "hit your limit") as caught:
            self.run_claude(json.dumps(result), code=1)
        self.assertEqual(type(caught.exception).__name__, "QuotaHit")

    def test_a_plain_text_limit_from_an_older_cli_raises_quota_hit_too(self):
        with self.assertRaisesRegex(Exception, "usage limit reached") as caught:
            self.run_claude("Claude AI usage limit reached|1764061200", code=1)
        self.assertEqual(type(caught.exception).__name__, "QuotaHit")


class Process(BudgetCase):
    """ops/run spends from the day's budget, and a quota or a spent day is never the node's fault."""

    def setUp(self):
        super().setUp()
        run = self.run = load_run()
        self.state = {"attempts": 0, "dead_ends": [], "saws": []}
        self.posts, self.caps = [], []
        wt = self.dir / "wt"
        wt.mkdir()
        run.AUTO = True
        run.prepare = lambda node: wt
        run.load_state, run.save_state = lambda _id: self.state, lambda _id, _s: None
        run.handoff_path = lambda _id: wt / "h.md"
        run._brief = types.SimpleNamespace(build=lambda node, note: "brief")
        run.gh = lambda *a: self.posts.append(a) or ""

    def node(self, repo="client"):
        body = "text\n```yaml\nfence: [src/*]\nblocked_by: []\nchecks: []\n```\n"
        agent = lib.AGENTS[repo]
        return lib.to_node(repo, {"number": 1, "title": "t", "body": body, "state": "OPEN",
                                  "labels": [{"name": f"agent:{agent}"}], "comments": []})

    def quota_agent(self, *a, **k):
        self.caps.append(a[3]["budget_usd"])
        self.run.LAST_COST = 0.4
        raise self.run.QuotaHit(LIMIT)

    def test_each_attempt_is_capped_at_what_is_left_of_the_day(self):
        self.spend(16.5)
        self.run.run_agent = self.quota_agent
        with self.assertRaises(self.run.QuotaHit):
            self.run.process(self.node(), {**CFG, "max_attempts": 3})
        self.assertEqual(self.caps, [3.5])

    def test_a_quota_gives_the_attempt_back_and_records_what_it_spent(self):
        self.run.run_agent = self.quota_agent
        with self.assertRaises(self.run.QuotaHit):
            self.run.process(self.node(), {**CFG, "max_attempts": 3})
        self.assertEqual(self.state["attempts"], 0)
        [attempt] = self.events("attempt")
        self.assertEqual((attempt["cost_usd"], attempt["stopped"]), (0.4, LIMIT))

    def test_a_spent_day_hands_the_node_back_before_any_attempt(self):
        self.spend(19.5)
        self.run.run_agent = lambda *a, **k: self.fail("no agent may start")
        with self.assertRaises(self.run.DailySpent):
            self.run.process(self.node(), {**CFG, "max_attempts": 3})
        self.assertEqual(self.state["attempts"], 0)

    def test_a_quota_hand_back_pauses_only_that_agent(self):
        self.run.hand_back(self.node("client"), self.run.QuotaHit(LIMIT), CFG)
        self.assertEqual(set(budget.paused()), {"claude"})
        [comment] = [p[-1] for p in self.posts if p[:2] == ("issue", "comment")]
        self.assertEqual(lib.parse_handoff(comment)["STATUS"], "paused")
        self.assertIn("resume by themselves", comment)
        [back] = self.events("handed_back")
        self.assertTrue(back["paused"])

    def test_a_paused_agent_holds_only_its_own_nodes(self):
        nodes = {"client#1": self.node("client"), "server#1": self.node("server")}
        ready = list(nodes)
        self.assertEqual(self.run.split_paused(ready, nodes, {"claude": {}}), (["server#1"], ["client#1"]))
        self.assertEqual(self.run.split_paused(ready, nodes, {}), (ready, []))
        self.assertEqual(self.run.split_paused(ready, nodes, {"unknown": {}}), ([], ready))

    def test_a_spent_day_holds_the_whole_queue(self):
        self.assertIsNone(self.run.queue_held(CFG))
        self.spend(19.5)
        self.assertRegex(self.run.queue_held(CFG), r"queue held: daily budget spent")

    def test_any_other_stop_still_needs_the_human(self):
        self.run.hand_back(self.node(), self.run.Stop("time budget spent"), CFG)
        self.assertEqual(budget.paused(), {})
        [comment] = [p[-1] for p in self.posts if p[:2] == ("issue", "comment")]
        self.assertEqual(lib.parse_handoff(comment)["STATUS"], "needs_human")

    def test_a_review_that_hits_the_quota_pauses_the_reviewer_and_still_reaches_the_human(self):
        self.run.run_agent = self.quota_agent
        self.run.sh = lambda cmd, cwd=None, check=True, env=None: subprocess.CompletedProcess(cmd, 0, "node/1-x\n", "")
        findings = self.run.independent_review(self.node("server"), str(self.dir), {**CFG, "timeout_min": 60})
        self.assertIsNone(findings)
        self.assertEqual(set(budget.paused()), {"claude"})  # Claude reviews the server
        self.assertIn("claude is paused until", self.run.REVIEW_UNAVAILABLE)
        [review] = self.events("review.run")
        self.assertEqual(review["cost_usd"], 0.4)

    def test_a_stopped_opencode_run_still_counts_what_it_spent(self):
        run = load_run()
        tmp = self.dir / "bin"
        tmp.mkdir()
        spend = json.dumps({"type": "step_finish", "part": {"cost": 2.5}})
        fake = tmp / "opencode"
        fake.write_text(f"#!/bin/sh\necho '{spend}'\necho '{spend}'\nsleep 60\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        run.STATE, run.HANDOFF_DIR = self.dir, self.dir / "handoff"
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{tmp}:{old_path}"
        try:
            with self.assertRaisesRegex(run.Stop, "cost budget"):
                run.run_agent("opencode", "p", git_worktree(self.dir / "repos"),
                              {"timeout_min": 5, "budget_usd": 3, "opencode_model": "m"})
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual((run.LAST_COST, run.LAST_COST_ESTIMATED), (5.0, False))


class Report(BudgetCase):
    def test_review_cost_adds_to_the_node_row(self):
        report = load_report()
        self.spend(1.5)
        self.spend(0.5, event="review.run")
        [row] = report.summarize(r for _, _, r in ledger.records())
        self.assertEqual(row["cost"], 2.0)

    def test_the_report_shows_today_against_the_budget_and_any_pause(self):
        report = load_report()
        self.spend(4.25)
        budget.pause_for_quota("claude", LIMIT, CFG)
        text = report.spending(CFG)
        self.assertIn("Spent today: $4.25 of $20", text)
        self.assertIn("claude paused until", text)


def load_report():
    loader = importlib.machinery.SourceFileLoader("report", str(OPS / "report"))
    spec = importlib.util.spec_from_loader("report", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
