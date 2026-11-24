"""Audit ledger (ops/ledger.py), ops/report, and the records ops/run writes."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from multiprocessing import Pool
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OPS))
import fakes
import ledger
import lib


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name, str(OPS / name))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def append_many(args):
    directory, count = args
    os.environ["OPS_LEDGER_DIR"] = directory
    for i in range(count):
        ledger.append("attempt", node="client#1", attempt=i)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        saved = os.environ.get("OPS_LEDGER_DIR")
        os.environ["OPS_LEDGER_DIR"] = str(self.dir)
        self.addCleanup(lambda: os.environ.__setitem__("OPS_LEDGER_DIR", saved) if saved
                        else os.environ.pop("OPS_LEDGER_DIR", None))

    def lines(self):
        [path] = ledger.files()
        return path, path.read_text().splitlines()


class Chain(LedgerCase):
    def test_records_chain_and_verify(self):
        first = ledger.append("run.started", node="server#9", agent="opencode")
        second = ledger.append("gate", node="server#9", tier=2, passed=True, failed=None)
        self.assertEqual((first["seq"], second["seq"]), (1, 2))
        self.assertEqual(first["prev"], ledger.GENESIS)
        self.assertEqual(second["prev"], first["hash"])
        self.assertNotIn("failed", second, "None fields are left out")
        self.assertEqual(ledger.verify(), ([], 2, second["hash"]))
        self.assertEqual(ledger.head(), (2, second["hash"]))

    def test_the_files_are_private_and_outside_the_repo(self):
        ledger.append("note", node="client#1")
        self.assertEqual(self.dir.stat().st_mode & 0o777, 0o700)
        os.environ.pop("OPS_LEDGER_DIR")
        self.assertFalse(str(ledger.ledger_dir()).startswith(str(OPS.parent)))
        self.assertTrue(str(ledger.ledger_dir()).endswith("talent-scout-ops"))

    def test_a_changed_line_is_caught(self):
        for i in range(3):
            ledger.append("attempt", node="client#1", cost_usd=1.0)
        path, lines = self.lines()
        record = json.loads(lines[1])
        record["cost_usd"] = 0.01
        lines[1] = json.dumps(record, sort_keys=True)
        path.write_text("\n".join(lines) + "\n")
        problems, _, _ = ledger.verify()
        self.assertTrue(any(":2: content changed" in p for p in problems), problems)

    def test_a_dropped_or_moved_line_is_caught(self):
        for i in range(4):
            ledger.append("attempt", node="client#1")
        path, lines = self.lines()
        path.write_text("\n".join([lines[0], lines[2], lines[3]]) + "\n")
        self.assertTrue(any("missing or moved" in p for p in ledger.verify()[0]))
        path.write_text("\n".join([lines[1], lines[0], lines[2], lines[3]]) + "\n")
        self.assertTrue(ledger.verify()[0])

    def test_a_cut_tail_is_caught_by_the_published_heads(self):
        for i in range(3):
            record = ledger.append("attempt", node="client#1")
        published = [(record["seq"], record["hash"])]
        path, lines = self.lines()
        path.write_text("\n".join(lines[:2]) + "\n")
        self.assertEqual(ledger.verify()[0], [], "a shorter chain is still a valid chain")
        problems, _, _ = ledger.verify(anchors=published)
        self.assertEqual(problems, ["published head seq 3 is missing in the local ledger"])

    def test_the_chain_runs_across_monthly_files(self):
        ledger.append("attempt", node="client#1")
        path, _ = self.lines()
        path.rename(self.dir / "audit-2000-01.jsonl")
        last = ledger.append("attempt", node="client#1")
        self.assertEqual(last["seq"], 2)
        self.assertEqual(len(ledger.files()), 2)
        self.assertEqual(ledger.verify()[0], [])

    def test_parallel_runners_keep_one_chain(self):
        with Pool(4) as pool:
            pool.map(append_many, [(str(self.dir), 25)] * 4)
        problems, count, _ = ledger.verify()
        self.assertEqual((problems, count), ([], 100))

    def test_anchors_are_found_in_published_text(self):
        record = ledger.append("ready", node="client#1")
        text = f"Closes #1\n\n{ledger.anchor()}"
        self.assertNotIn("audit-ledger-head", text.split("<!--")[0], "hidden in the rendered body")
        self.assertEqual(ledger.anchors_in([text, None, "nothing"]), [(1, record["hash"])])


class Report(LedgerCase):
    def node_run(self, node, tier=1, interventions=0, escaped=False, red=False, gate_first=True):
        ledger.append("run.started", node=node)
        ledger.append("attempt", node=node, cost_usd=0.5)
        ledger.append("gate", node=node, tier=tier, passed=gate_first)
        for _ in range(interventions):
            ledger.append("escalated", node=node, reason="x")
        ledger.append("ready", node=node)
        ledger.append("merged", node=node, by="aangwijaya", ci_red=["test"] if red else None)
        if escaped:
            ledger.append("note", node=node, kind="escaped-high", text="verifier found it")

    def report(self):
        return load_script("report")

    def test_a_row_per_node(self):
        self.node_run("client#5", tier=2, interventions=1, gate_first=False)
        report = self.report()
        [row] = report.summarize(r for _, _, r in ledger.records())
        self.assertEqual((row["tier"], row["attempts"], row["first_gate"], row["interventions"], row["cost"],
                          row["outcome"], row["merged_by"]), (2, 1, False, 1, 0.5, "merged", "aangwijaya"))
        self.assertIn("| client#5 | 2 | 1 | fail |", report.table([row]))

    def test_tier1_targets_need_ten_clean_merges(self):
        report = self.report()
        for i in range(9):
            self.node_run(f"client#{i}")
        met, lines = report.tier1_readiness(report.summarize(r for _, _, r in ledger.records()))
        self.assertFalse(met)
        self.assertIn("9/10", lines[0])
        self.node_run("client#9")
        met, _ = report.tier1_readiness(report.summarize(r for _, _, r in ledger.records()))
        self.assertTrue(met)

    def test_one_escaped_high_or_red_merge_fails_the_targets(self):
        report = self.report()
        for i in range(10):
            self.node_run(f"client#{i}", escaped=i == 3)
        self.assertFalse(report.tier1_readiness(report.summarize(r for _, _, r in ledger.records()))[0])
        for i in range(10, 20):
            self.node_run(f"client#{i}", red=i == 15)
        self.assertFalse(report.tier1_readiness(report.summarize(r for _, _, r in ledger.records()))[0])

    def test_tier2_nodes_do_not_count_toward_tier1(self):
        report = self.report()
        for i in range(10):
            self.node_run(f"server#{i}", tier=2)
        self.assertFalse(report.tier1_readiness(report.summarize(r for _, _, r in ledger.records()))[0])

    def test_note_and_verify_from_the_command_line(self):
        env = {**os.environ, "OPS_LEDGER_DIR": str(self.dir)}
        run = lambda *a: subprocess.run([str(OPS / "report"), *a], env=env, capture_output=True, text=True)
        noted = run("--note", "client#12", "intervention", "fixed the lint by hand", "--by", "lead")
        self.assertEqual(noted.returncode, 0, noted.stderr)
        self.assertEqual(run("--note", "client#12", "bogus", "x").returncode, 1)
        [record] = [r for _, _, r in ledger.records()]
        self.assertEqual((record["actor"], record["kind"]), ("lead", "intervention"))
        self.assertEqual(run("--verify", "--offline").returncode, 0)
        path, lines = self.lines()
        path.write_text(lines[0].replace("by hand", "by agent") + "\n")
        broken = run("--verify", "--offline")
        self.assertEqual(broken.returncode, 1)
        self.assertIn("content changed", broken.stdout)


class RunRecords(LedgerCase):
    """ops/run writes one record per step, and every PR and handoff carries the head."""

    def setUp(self):
        super().setUp()
        self.run = load_script("run")
        self.gh_calls = []

        def fake_gh(*args):
            self.gh_calls.append(args)
            return "https://github.com/o/r/pull/1\n" if "url" in args else ""
        self.run.gh = fake_gh

    def node(self, num=1):
        body = "text\n```yaml\nfence: [src/*]\nblocked_by: []\nchecks: []\n```\n"
        return lib.to_node("client", {"number": num, "title": "t", "body": body, "state": "OPEN",
                                      "labels": [], "comments": []})

    def test_a_clean_run_is_recorded_step_by_step(self):
        run, tmp = self.run, Path(self.dir, "wt")
        tmp.mkdir()
        handoff = tmp / "h.md"
        state = {"attempts": 0, "dead_ends": [], "saws": []}
        run.AUTO = True
        run.prepare = lambda node: tmp
        run.load_state, run.save_state = lambda _id: state, lambda _id, _s: None
        run.handoff_path = lambda _id: handoff
        run._brief = types.SimpleNamespace(build=lambda node, note: "brief")

        def agent(*a, **k):
            handoff.write_text("STATUS: done\nGOAL: g\nTRIED: t\nKNOWN: k\nNEXT: n\nNEVER: x\n")
            return 0, ""
        run.run_agent = agent
        run.gate = lambda node, wt, cfg, text: {"failures": [], "tier": 2}
        run.sh = lambda cmd, cwd=None, check=True, env=None: subprocess.CompletedProcess(
            cmd, 1 if cmd[:3] == ["gh", "pr", "view"] else 0, "node/1-x\n", "")
        run.wait_ci = lambda node, branch, cfg: []
        run.independent_review = lambda node, wt, cfg: [{"severity": "Low", "file": "a", "summary": "b"}]
        run.process(self.node(), {"max_attempts": 3, "automerge": [], "budget_usd": 6, "daily_budget_usd": 20})

        events = [r["event"] for _, _, r in ledger.records()]
        self.assertEqual(events, ["run.started", "attempt", "gate", "pr.opened", "ci", "review", "ready"])
        records = {r["event"]: r for _, _, r in ledger.records()}
        self.assertEqual((records["gate"]["tier"], records["gate"]["passed"]), (2, True))
        self.assertEqual((records["review"]["findings"], records["review"]["high"]), (1, 0))
        self.assertTrue(all(r["actor"] == "orchestrator" and r["node"] == "client#1" for r in records.values()))
        [create] = [c for c in self.gh_calls if c[:2] == ("pr", "create")]
        self.assertRegex(create[-1], r"audit-ledger-head: seq 4 sha256 [0-9a-f]{64}")
        comments = [c[-1] for c in self.gh_calls if c[:2] == ("issue", "comment")]
        self.assertTrue(comments and all("audit-ledger-head: seq" in c for c in comments))
        self.assertTrue(all(lib.parse_handoff(c).get("STATUS") for c in comments), "the head never breaks a handoff")
        self.assertEqual(ledger.verify()[0], [])

    def test_escalation_and_hand_back_count_as_interventions(self):
        self.run.escalate(self.node(), "the same SAW came back twice")
        [record] = [r for _, _, r in ledger.records()]
        self.assertEqual((record["event"], record["reason"]), ("escalated", "the same SAW came back twice"))

    def test_a_merge_records_who_merged_and_red_checks(self):
        rollup = {"number": 7, "mergedBy": {"login": "aangwijaya"},
                  "statusCheckRollup": [{"name": "test", "conclusion": "SUCCESS"},
                                        {"name": "contract", "conclusion": "FAILURE"}]}
        self.run.gh = lambda *a: json.dumps(rollup)
        self.assertEqual(self.run.pr_merge_info(self.node(), "node/1-x"),
                         {"pr": 7, "by": "aangwijaya", "ci_red": ["contract"]})

        def broken(*a):
            raise RuntimeError("no PR")
        self.run.gh = broken
        self.assertIn("lookup_error", self.run.pr_merge_info(self.node(), "node/1-x"))


if __name__ == "__main__":
    unittest.main()
