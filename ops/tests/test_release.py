"""The integration branch, auto-merge rules, the verifier step, reverts and releases (ADR 0008)."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OPS))
import fakes
import ledger
import lib
import merge_policy

DEV = {"integration_branch": "dev", "automerge": ["1", "2"], "max_attempts": 3, "budget_usd": 6,
       "daily_budget_usd": 20, "timeout_min": 60}
MERGE = {"verdict": "merge", "criteria": "3/3 met", "high": [], "gaps": [], "summary": "fine"}


def setUpModule():
    fakes.sandbox_for_tests(tempfile.mkdtemp(prefix="ops-sandbox-"))


def load(name):
    loader = importlib.machinery.SourceFileLoader(name, str(OPS / name))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    if name == "run":
        token = Path(tempfile.mkdtemp(prefix="ops-token-"), "agent.gh-token")
        token.write_text("github_pat_" + "x" * 40 + "\n")
        module.AGENT_GH_TOKEN = token  # never the real one
    return module


def node(num=1, repo="client"):
    body = "text\n```yaml\nfence: [src/*]\nblocked_by: []\nchecks: []\n```\n"
    return lib.to_node(repo, {"number": num, "title": "t", "body": body, "state": "OPEN",
                              "labels": [{"name": f"agent:{lib.AGENTS[repo]}"}], "comments": []})


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        saved = os.environ.get("OPS_LEDGER_DIR")
        os.environ["OPS_LEDGER_DIR"] = str(self.dir / "ledger")
        self.addCleanup(lambda: os.environ.__setitem__("OPS_LEDGER_DIR", saved) if saved
                        else os.environ.pop("OPS_LEDGER_DIR", None))
        old = merge_policy.STATE, merge_policy.HOLD_FILE
        merge_policy.STATE, merge_policy.HOLD_FILE = self.dir / "state", self.dir / "state" / "automerge-off.json"
        self.addCleanup(lambda: setattr(merge_policy, "STATE", old[0]) or setattr(merge_policy, "HOLD_FILE", old[1]))

    def events(self, name):
        return [r for _, _, r in ledger.records() if r["event"] == name]


class Policy(Case):
    def test_tiers_1_and_2_merge_by_themselves_into_dev_only(self):
        self.assertEqual(merge_policy.tiers(DEV), [1, 2])
        self.assertEqual(merge_policy.tiers({**DEV, "integration_branch": "main"}), [])
        self.assertIn("until the integration branch exists", merge_policy.why_not(1, {**DEV, "integration_branch": "main"}))
        self.assertEqual(merge_policy.why_not(3, {**DEV, "automerge": ["1", "2", "3"]}), "tier 3 always needs the human")

    def test_an_escaped_high_turns_tier2_off_once_until_the_human_turns_it_on(self):
        merge_policy.escaped_high("server#4", "auth skipped", by="verifier")
        merge_policy.escaped_high("server#5", "another", by="verifier")
        self.assertEqual(merge_policy.tiers(DEV), [1])
        self.assertIn("escaped High in server#4", merge_policy.why_not(2, DEV))
        self.assertEqual(len(self.events("automerge.off")), 1)
        self.assertTrue(merge_policy.turn_on())
        self.assertEqual(merge_policy.tiers(DEV), [1, 2])
        self.assertEqual(self.events("automerge.on")[0]["actor"], "human")

    def test_report_note_and_automerge_on_from_the_command_line(self):
        code = ("import sys, pathlib; sys.path.insert(0, %r); import merge_policy;"
                "merge_policy.STATE=pathlib.Path(%r); merge_policy.HOLD_FILE=merge_policy.STATE/'automerge-off.json';"
                "import runpy; sys.argv=['report', *%r]; runpy.run_path(%r, run_name='__main__')")
        run = lambda *args: subprocess.run([sys.executable, "-c", code % (str(OPS), str(merge_policy.STATE), list(args),
                                                                           str(OPS / "report"))],
                                           capture_output=True, text=True, env=os.environ)
        out = run("--note", "client#7", "escaped-high", "XSS in the preview")
        self.assertIn("auto-merge is off", out.stdout, out.stderr)
        self.assertEqual(merge_policy.tiers(DEV), [1])
        self.assertIn("back on", run("--automerge", "on").stdout)
        self.assertEqual(merge_policy.tiers(DEV), [1, 2])


class Verdict(Case):
    def setUp(self):
        super().setUp()
        self.run = load("run")

    def test_the_verdict_is_read_fenced_or_bare_and_must_be_known(self):
        p = self.run.parse_verdict
        self.assertEqual(p('notes\n```json\n' + json.dumps(MERGE) + '\n```')["verdict"], "merge")
        self.assertEqual(p('I ran it. {"verdict": "rework", "high": []}')["verdict"], "rework")
        self.assertIsNone(p('{"verdict": "ship it"}'))
        self.assertIsNone(p("no block"))

    def test_a_high_the_review_missed_is_escaped_and_goes_back_to_the_agent(self):
        report = {**MERGE, "verdict": "rework", "high": [{"file": "a.go", "summary": "no auth", "review_had_it": False},
                                                          {"file": "b.go", "summary": "race", "review_had_it": True}]}
        failures, _ = self.run.weigh_verdict(node(), report, [{"severity": "High"}], 2)
        self.assertEqual([f["check"] for f in failures], ["verify", "verify"])
        [note] = self.events("note")
        self.assertEqual((note["kind"], note["actor"]), ("escaped-high", "verifier"))
        self.assertEqual(merge_policy.tiers(DEV), [1])

    def test_tier1_has_no_review_so_nothing_escapes_it(self):
        report = {**MERGE, "high": [{"file": "a.md", "summary": "x", "review_had_it": False}]}
        self.run.weigh_verdict(node(), report, None, 1)
        self.assertEqual(self.events("note"), [])
        self.assertEqual(merge_policy.tiers(DEV), [1, 2])


class Process(Case):
    """After CI and the review, the verifier decides whether a tier 1–2 node merges into dev by itself."""

    def setUp(self):
        super().setUp()
        run = self.run = load("run")
        self.calls = []
        wt = self.dir / "wt"
        wt.mkdir()
        handoff = wt / "h.md"
        state = {"attempts": 0, "dead_ends": [], "saws": []}
        run.AUTO = True
        run.prepare = lambda n: wt
        run.load_state, run.save_state = lambda _id: state, lambda _id, _s: None
        run.handoff_path = lambda _id: handoff
        run._brief = types.SimpleNamespace(build=lambda n, note: "brief")
        run.gh = lambda *a: self.calls.append(a) or ("https://github.com/o/r/pull/1\n" if "url" in a else "")

        def agent(*a, **k):
            handoff.write_text("STATUS: done\nGOAL: g\nTRIED: t\nKNOWN: k\nNEXT: n\nNEVER: x\n")
            return 0, ""
        run.run_agent = agent
        self.tier = 2
        run.gate = lambda n, wt, cfg, text, base=None: self.seen_base.append(base) or {"failures": [], "tier": self.tier}
        self.seen_base = []
        run.sh = lambda cmd, cwd=None, check=True, env=None: subprocess.CompletedProcess(
            cmd, 1 if cmd[:3] == ["gh", "pr", "view"] else 0, "node/1-x\n", "")
        run.wait_ci = lambda n, branch, cfg: []
        run.independent_review = lambda n, wt, cfg: []
        self.report = MERGE
        run.verify = lambda n, wt, cfg, findings: self.report

    def merged(self):
        return [c for c in self.calls if c[:2] == ("pr", "merge")]

    def test_a_tier2_node_with_a_merge_verdict_merges_into_dev(self):
        self.run.process(node(), DEV)
        self.assertEqual(len(self.merged()), 1)
        [record] = self.events("merged")
        self.assertEqual((record["by"], record["into"]), ("orchestrator", "dev"))
        self.assertEqual(self.seen_base, ["origin/dev"])
        [create] = [c for c in self.calls if c[:2] == ("pr", "create")]
        self.assertEqual(create[create.index("--base") + 1], "dev")

    def test_until_dev_exists_everything_waits_for_the_human(self):
        self.run.process(node(), {**DEV, "integration_branch": "main"})
        self.assertEqual(self.merged(), [])
        self.assertEqual(len(self.events("ready")), 1)

    def test_tier3_waits_for_the_human_even_into_dev(self):
        self.tier = 3
        self.run.process(node(), {**DEV, "automerge": ["1", "2", "3"]})
        self.assertEqual(self.merged(), [])
        self.assertIn("tier 3 always needs the human", self.events("ready")[0]["reason"])

    def test_any_verdict_but_merge_waits_for_the_human(self):
        self.report = {**MERGE, "verdict": "fix-small", "gaps": ["drop the guard in x.ts"]}
        self.run.process(node(), DEV)
        self.assertEqual(self.merged(), [])
        self.assertIn("the verifier said fix-small", self.events("ready")[0]["reason"])

    def test_no_verdict_means_no_merge(self):
        self.report = None
        self.run.VERIFY_UNAVAILABLE = "time budget spent"
        self.run.process(node(), DEV)
        self.assertEqual(self.merged(), [])
        self.assertIn("the verifier was unavailable", self.events("ready")[0]["reason"])

    def test_tier2_stops_merging_after_an_escaped_high(self):
        merge_policy.escaped_high("client#9", "x", by="human")
        self.run.process(node(), DEV)
        self.assertEqual(self.merged(), [])


class VerifierRun(Case):
    def test_the_verifier_works_in_a_fresh_copy_and_nothing_comes_back(self):
        run = load("run")
        repos = self.dir / "repos"
        wt = fakes.git_worktree(repos)
        stale = fakes.container.CLONES / f"{wt.name}-verify"
        stale.mkdir(parents=True)
        (stale / "left-over").write_text("from the last verification")
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text("#!/bin/sh\ncat > /dev/null\nls left-over 2>/dev/null && echo STALE\necho sabotage > a.txt\n"
                        "echo '{\"type\": \"result\", \"subtype\": \"success\", \"result\": \"{\\\"verdict\\\": \\\"merge\\\"}\", "
                        "\"total_cost_usd\": 0.5}'\n")
        fake.chmod(0o755)
        original = run.container.import_agent_commits
        self.addCleanup(setattr, run.container, "import_agent_commits", original)
        run.container.import_agent_commits = lambda *a: self.fail("the verifier's changes must never come back")
        run.STATE, run.HANDOFF_DIR = self.dir, self.dir / "handoff"
        old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}:{old_path}"
        try:
            code, text = run.run_agent("claude", "p", wt, {"timeout_min": 1, "budget_usd": 1, "claude_model": "c",
                                                            "claude_effort": "low"}, verify=True)
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(run.parse_verdict(text)["verdict"], "merge")
        self.assertEqual((wt / "a.txt").read_text(), "base\n", "the human's worktree is untouched")
        self.assertFalse((stale / "left-over").exists(), "every verification starts from a fresh copy")
        log = Path(fakes.container.PODMAN).with_name("podman.log").read_text().splitlines()
        [line] = [l for l in log if " claude -p " in l and "-verify" in l][-1:]
        self.assertNotIn("Bash(git commit:*)", line)
        self.assertIn("Bash(git checkout:*)", line)


class Revert(unittest.TestCase):
    PRS = [{"number": 10, "headRefName": "node/5-a", "mergedAt": "2026-11-25T01:00:00Z", "mergeCommit": {"oid": "a" * 40}},
           {"number": 11, "headRefName": "node/6-b", "mergedAt": "2026-11-25T02:00:00Z", "mergeCommit": {"oid": "b" * 40}},
           {"number": 12, "headRefName": "contract/abc", "mergedAt": "2026-11-25T03:00:00Z", "mergeCommit": {"oid": "c" * 40}},
           {"number": 9, "headRefName": "node/4-c", "mergedAt": "2026-11-24T01:00:00Z", "mergeCommit": {"oid": "d" * 40}}]

    def test_the_nodes_merged_after_it_get_a_second_look(self):
        revert = load("revert")
        pr = revert.node_pr(self.PRS, 5)
        self.assertEqual(pr["number"], 10)
        self.assertEqual(revert.later_nodes(self.PRS, pr), [6])
        with self.assertRaises(SystemExit):
            revert.node_pr(self.PRS, 99)


class Release(Case):
    def test_the_body_is_one_row_per_node_from_the_ledger_with_the_steps_it_needs(self):
        release = load("release")
        ledger.append("gate", actor="orchestrator", node="server#5", tier=2, passed=True)
        ledger.append("attempt", actor="orchestrator", node="server#5", cost_usd=1.25)
        ledger.append("review", actor="orchestrator", node="server#5", available=True, findings=2, high=0)
        ledger.append("verify", actor="orchestrator", node="server#5", available=True, verdict="merge")
        ledger.append("merged", actor="orchestrator", node="server#5", by="orchestrator")
        prs = [{"number": 10, "title": "Scorer prompt", "headRefName": "node/5-scorer",
                "files": [{"path": "internal/prompts/prompts.go"}, {"path": "internal/store/migrations/00012_x.sql"}]}]
        text = release.body("server", DEV, prs, summary="Voice, part one.")
        self.assertTrue(text.startswith("Voice, part one."))
        self.assertIn("| #10 | #5 | Scorer prompt | 2 | 2 findings, 0 High | merge | orchestrator | $1.25 |", text)
        self.assertIn("Live gate", text)
        self.assertIn("run the new migrations in production", text)
        self.assertIn("audit-ledger-head: seq", text)

    def test_nothing_to_release_while_integration_is_main(self):
        release = load("release")
        release.config = lambda: {"integration_branch": "main"}
        with self.assertRaisesRegex(SystemExit, "nothing to release"):
            release.main(["release", "server"])

    def test_red_ci_or_an_unreleased_server_contract_blocks_the_release(self):
        release = load("release")
        release.config = lambda: DEV
        release.ci_red = lambda repo, cfg: ["test (failure)"]
        release.contract_released = lambda cfg: "server 1234abcd (pinned by contract.lock) is not in the server's main yet"
        release.pending_prs = lambda repo, cfg: [{"number": 3, "title": "x", "headRefName": "node/3-x", "files": []}]
        opened = []
        release.gh = lambda *a: opened.append(a) or ""
        self.assertEqual(release.main(["release", "client", "--open"]), 1)
        self.assertEqual(opened, [], "no PR while a condition fails")


if __name__ == "__main__":
    unittest.main()
