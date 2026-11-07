"""Run with: python3 -m unittest discover -s ops/tests"""
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OPS))
import checks
import lib


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name, str(OPS / name))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def node(num, repo="client", closed=False, labels=(), block="fence: [src/*]\nblocked_by: []\nchecks: []\n", comments=()):
    body = f"text\n```yaml\n{block}```\n" if block else "no block"
    return lib.to_node(repo, {"number": num, "title": f"t{num}", "body": body,
                              "state": "CLOSED" if closed else "OPEN",
                              "labels": [{"name": l} for l in labels],
                              "comments": [{"body": c} for c in comments]})


def graph_of(*nodes):
    return lib.analyze({n.id: n for n in nodes})


GOOD_HANDOFF = "STATUS: done\nGOAL: g\nTRIED: t\nKNOWN: k\nNEXT: n\nNEVER: x\n"
RED_BLOCK = "fence: [src/*, tests/*]\nblocked_by: []\nchecks: [grep -q implemented src/code.txt]\n"


class Graph(unittest.TestCase):
    def test_closed_dependency_makes_a_node_ready(self):
        g = graph_of(node(1, closed=True), node(2, block="fence: [a]\nblocked_by: [client#1]\n"))
        self.assertEqual(g["ready"], ["client#2"])

    def test_open_dependency_waits_across_repos(self):
        g = graph_of(node(1, repo="server"), node(2, block="fence: [a]\nblocked_by: [server#1]\n"))
        self.assertEqual(g["ready"], ["server#1"])
        self.assertEqual(g["waiting"], [{"id": "client#2", "on": ["server#1"]}])

    def test_unknown_dependency_never_counts_as_done(self):
        g = graph_of(node(2, block="fence: [a]\nblocked_by: [client#99]\n"))
        self.assertEqual(g["ready"], [])

    def test_gap_runs_before_everything_else(self):
        g = graph_of(node(1), node(9, labels=["type:gap"]))
        self.assertEqual(g["ready"][0], "client#9")

    def test_node_that_unblocks_more_runs_first(self):
        g = graph_of(node(5), node(6), node(7, block="fence: [a]\nblocked_by: [client#6]\n"))
        self.assertEqual(g["ready"], ["client#6", "client#5"])

    def test_cycle_is_reported_and_never_ready(self):
        g = graph_of(node(1, block="fence: [a]\nblocked_by: [client#2]\n"),
                     node(2, block="fence: [a]\nblocked_by: [client#1]\n"))
        self.assertEqual(sorted(g["cycles"]), ["client#1", "client#2"])
        self.assertEqual(g["ready"], [])

    def test_human_blocked_and_unmapped_are_not_ready(self):
        g = graph_of(node(1, labels=["agent:human"]), node(2, labels=["needs:human"]),
                     node(3, labels=["status:blocked"]), node(4, block=None))
        self.assertEqual(g["ready"], [])
        self.assertEqual(g["unmapped"], ["client#4"])
        self.assertEqual(sorted(g["human"]), ["client#1", "client#2", "client#3"])

    def test_active_nodes_are_not_picked_again(self):
        self.assertEqual(graph_of(node(1, labels=["status:in-progress"]))["active"], ["client#1"])


class Tiers(unittest.TestCase):
    cfg = lib.config()

    def test_tier3_path_wins_over_a_docs_only_look(self):
        self.assertEqual(lib.tier_of(["README.md", "docs/API.md"], self.cfg), 3)
        self.assertEqual(lib.tier_of(["src/hooks.server.ts"], self.cfg), 3)
        self.assertEqual(lib.tier_of(["internal/store/migrations/00002.sql"], self.cfg), 3)

    def test_an_agent_cannot_loosen_its_own_rules_below_tier3(self):
        for path in ("CLAUDE.md", "AGENTS.md", ".claude/settings.json", ".github/workflows/ci.yml"):
            self.assertEqual(lib.tier_of([path], self.cfg), 3, path)

    def test_docs_and_tests_only_is_tier1(self):
        self.assertEqual(lib.tier_of(["docs/x.md", "src/a.test.ts", "internal/a_test.go"], self.cfg), 1)

    def test_mixed_code_is_tier2(self):
        self.assertEqual(lib.tier_of(["src/routes/a.svelte", "src/a.test.ts"], self.cfg), 2)


class Gate(unittest.TestCase):
    """Each sabotage must be rejected; the clean change must pass."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = self.tmp.name
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.write("src/code.txt", "old\n")
        self.write("docs/API.md", "v1\n")
        self.commit("base")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("checkout", "-q", "-b", "work")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.wt, check=True, capture_output=True)

    def write(self, path, text):
        target = Path(self.wt, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message="c"):
        self.git("add", "-A")
        self.git("commit", "-qm", message)

    def run_gate(self, block=RED_BLOCK, labels=(), handoff=GOOD_HANDOFF, repo="server"):
        n = node(1, repo=repo, labels=labels, block=block)
        return checks.gate(n, self.wt, lib.config(), handoff, run_green=False)

    def failed(self, result):
        return {f["check"] for f in result["failures"]}

    def good_change(self):
        self.write("src/code.txt", "implemented\n")
        self.write("tests/a_test.go", "test\n")
        self.commit()

    def test_clean_change_passes(self):
        self.good_change()
        result = self.run_gate()
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["tier"], 2)

    def test_rejects_a_diff_outside_the_fence(self):
        self.good_change()
        self.write("elsewhere/x.txt", "x\n")
        self.commit()
        self.assertIn("fence", self.failed(self.run_gate()))

    def test_rejects_a_test_that_never_fails(self):
        self.good_change()
        result = self.run_gate(block="fence: [src/*, tests/*]\nchecks: [true]\n")
        self.assertIn("red", self.failed(result))

    def test_rejects_code_without_tests(self):
        self.write("src/code.txt", "implemented\n")
        self.commit()
        self.assertIn("red", self.failed(self.run_gate()))

    def test_rejects_a_fake_credential(self):
        self.good_change()
        self.write("src/config.txt", "key = sk-or-v1-abcdefghijklmnop\n")
        self.commit()
        self.assertIn("secrets", self.failed(self.run_gate()))

    def test_rejects_a_committed_env_file(self):
        self.good_change()
        self.git("add", "-A")
        self.write("src/.env", "A=1\n")
        self.commit()
        self.assertIn("secrets", self.failed(self.run_gate()))

    def test_rejects_contract_edit_without_label(self):
        self.good_change()
        self.write("docs/API.md", "v2\n")
        self.commit()
        block = "fence: [src/*, tests/*, docs/*]\nchecks: [grep -q implemented src/code.txt]\n"
        self.assertIn("contract", self.failed(self.run_gate(block=block)))

    def test_allows_contract_edit_with_label_as_tier3(self):
        self.good_change()
        self.write("docs/API.md", "v2\n")
        self.commit()
        block = "fence: [src/*, tests/*, docs/*]\nchecks: [grep -q implemented src/code.txt]\n"
        result = self.run_gate(block=block, labels=["contract-change"])
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["tier"], 3)

    def test_rejects_an_incomplete_handoff(self):
        self.good_change()
        self.assertIn("handoff", self.failed(self.run_gate(handoff="STATUS: done\nGOAL: g\n")))

    def test_rejects_an_empty_diff(self):
        self.assertIn("diff", self.failed(self.run_gate()))

    def test_docs_only_change_skips_red(self):
        self.write("docs/notes.md", "x\n")
        self.commit()
        result = self.run_gate(block="fence: [docs/*]\nchecks: []\n")
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["tier"], 1)

    def test_red_check_leaves_no_worktree_behind(self):
        self.good_change()
        self.run_gate()
        out = subprocess.run(["git", "worktree", "list"], cwd=self.wt, capture_output=True, text=True).stdout
        self.assertEqual(len(out.strip().splitlines()), 1)


class Loop(unittest.TestCase):
    def test_same_failure_with_different_timing_counts_as_same_saw(self):
        a = [{"check": "green", "saw": "FAIL x (1.20s)"}]
        b = [{"check": "green", "saw": "FAIL x (3.75s)"}]
        self.assertEqual(checks.saw_key(a), checks.saw_key(b))

    def test_different_failure_has_a_different_saw(self):
        a = [{"check": "green", "saw": "FAIL x"}]
        b = [{"check": "green", "saw": "FAIL y"}]
        self.assertNotEqual(checks.saw_key(a), checks.saw_key(b))

    def test_feedback_has_the_structured_fields(self):
        text = checks.feedback([{"check": "c", "expected": "e", "saw": "s"}], {"fence": ["a"]}, ["d"])
        for field in ("EXPECTED: e", "SAW: s", "FENCE: ['a']", "DEAD ENDS: d"):
            self.assertIn(field, text)


class Handoff(unittest.TestCase):
    def test_parse_keeps_multiline_fields(self):
        fields = lib.parse_handoff("STATUS: gap\nKNOWN: a\nb\nGAPS: client#3\n")
        self.assertEqual(fields["KNOWN"], "a\nb")
        self.assertEqual(fields["GAPS"], "client#3")

    def test_new_session_resumes_from_the_brief_alone(self):
        previous = f"{lib.HANDOFF_MARK}\nSTATUS: done\nNEXT: finish the retry test"
        n = node(7, labels=["agent:claude"], comments=["chatter", previous])
        brief = load_script("brief").build(n, feedback="SAW: boom")
        for part in ("finish the retry test", "SAW: boom", "client#7", "Handoff file:", "SCOPE"):
            self.assertIn(part, brief)


class Learning(unittest.TestCase):
    def test_repeated_lesson_without_a_follow_up_pr_is_found(self):
        triage_src = (OPS / "triage").read_text().replace("\nmain()\n", "\n")
        scope = {"__file__": str(OPS / "triage")}
        exec(compile(triage_src, "triage", "exec"), scope)
        text = ("## Entry template\n### YYYY\n- **Repeated?:** first time | same as entry\n## Entries\n"
                "### 2026 A\n- **Follow-up:** pending PR\n- **Repeated?:** same root cause\n"
                "### 2026 B\n- **Follow-up:** skill-update PR #9\n- **Repeated?:** same as X\n"
                "### 2026 C\n- **Follow-up:** none\n- **Repeated?:** first time\n")
        self.assertEqual(scope["repeated_lessons"](text), ["2026 A"])


class KillSwitch(unittest.TestCase):
    def test_stop_terminates_a_running_agent(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp, "opencode")
            fake.write_text("#!/bin/sh\nsleep 60\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            stop_file = Path(tmp, "STOP")
            old_path, old_stop = os.environ["PATH"], run.STOP_FILE
            os.environ["PATH"] = f"{tmp}:{old_path}"
            run.STOP_FILE = stop_file
            run.STATE = Path(tmp)
            threading.Timer(1, stop_file.touch).start()
            started = time.time()
            try:
                with self.assertRaises(run.Stop):
                    run.run_agent("opencode", "p", tmp, {"timeout_min": 5, "budget_usd": 1, "opencode_model": "fake/model"})
            finally:
                os.environ["PATH"], run.STOP_FILE = old_path, old_stop
            self.assertLess(time.time() - started, 15)

    def test_large_output_does_not_block_and_stop_kills_child_processes(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp, "opencode")
            marker = Path(tmp, "survived")
            fake.write_text(f"#!/bin/sh\nhead -c 300000 /dev/zero | tr '\\0' x\necho done\n"
                            f"(sleep 4; touch {marker}) &\nsleep 60\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            old_path, old_stop = os.environ["PATH"], run.STOP_FILE
            os.environ["PATH"] = f"{tmp}:{old_path}"
            run.STOP_FILE = Path(tmp, "STOP")
            run.STATE = Path(tmp)
            threading.Timer(1, run.STOP_FILE.touch).start()
            try:
                with self.assertRaises(run.Stop):
                    run.run_agent("opencode", "p", tmp, {"timeout_min": 5, "budget_usd": 1, "opencode_model": "fake/model"})
            finally:
                os.environ["PATH"], run.STOP_FILE = old_path, old_stop
            time.sleep(5)
            self.assertFalse(marker.exists())

    def test_agent_output_over_the_pipe_size_is_returned(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp, "opencode")
            fake.write_text("#!/bin/sh\nhead -c 300000 /dev/zero | tr '\\0' x\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            old_path = os.environ["PATH"]
            os.environ["PATH"] = f"{tmp}:{old_path}"
            run.STATE = Path(tmp)
            try:
                code, text = run.run_agent("opencode", "p", tmp, {"timeout_min": 1, "budget_usd": 1, "opencode_model": "fake/model"})
            finally:
                os.environ["PATH"] = old_path
            self.assertEqual((code, len(text)), (0, 300000))

    def test_opencode_runs_in_the_worktree_and_may_read_the_go_module_cache(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as wt:
            fake = Path(tmp, "opencode")
            fake.write_text('#!/bin/sh\necho "PWD=$PWD"\necho "ARGS=$*"\necho "CFG=$OPENCODE_CONFIG_CONTENT"\n')
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            old_path = os.environ["PATH"]
            os.environ["PATH"] = f"{tmp}:{old_path}"
            run.STATE, run.HANDOFF_DIR = Path(tmp), Path(tmp, "handoff")
            try:
                _, text = run.run_agent("opencode", "p", wt, {"timeout_min": 1, "budget_usd": 1, "opencode_model": "fake/model"})
            finally:
                os.environ["PATH"] = old_path
            self.assertIn(f"PWD={wt}\n", text)
            self.assertIn(f"--dir {wt}", text)
            self.assertIn("--format json", text)
            cfg = json.loads(text.split("CFG=", 1)[1])
            allowed = cfg["permission"]["external_directory"]
            self.assertEqual(set(allowed.values()), {"allow"})
            self.assertIn(f"{tmp}/handoff/*", allowed)
            self.assertNotIn(f"{tmp}/*", allowed)
            self.assertTrue(any(k.endswith("/pkg/mod/**") or k.endswith("/mod/**") for k in allowed))

    def fake_opencode(self, tmp, script):
        fake = Path(tmp, "opencode")
        fake.write_text("#!/bin/sh\n" + script)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        return load_script("run")

    def test_opencode_events_give_the_reply_text_and_a_readable_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = [{"type": "step_start", "part": {}},
                      {"type": "tool_use", "part": {"tool": "bash", "state": {"title": "go test ./...", "status": "completed"}}},
                      {"type": "text", "part": {"text": "done"}},
                      {"type": "step_finish", "part": {"cost": 0.01}}]
            lines = "".join(f"echo '{json.dumps(e)}'\n" for e in events)
            run = self.fake_opencode(tmp, lines)
            run.STATE, run.HANDOFF_DIR = Path(tmp), Path(tmp, "handoff")
            old_path = os.environ["PATH"]
            os.environ["PATH"] = f"{tmp}:{old_path}"
            try:
                code, text = run.run_agent("opencode", "p", tmp, {"timeout_min": 1, "budget_usd": 1, "opencode_model": "m"})
            finally:
                os.environ["PATH"] = old_path
            self.assertEqual((code, text), (0, "done"))
            self.assertIn("→ bash go test ./... [completed]", Path(tmp, Path(tmp).name + ".log").read_text())

    def test_opencode_is_stopped_when_it_spends_the_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            spend = json.dumps({"type": "step_finish", "part": {"cost": 2.5}})
            run = self.fake_opencode(tmp, f"echo '{spend}'\necho '{spend}'\nsleep 60\n")
            run.STATE, run.HANDOFF_DIR = Path(tmp), Path(tmp, "handoff")
            old_path = os.environ["PATH"]
            os.environ["PATH"] = f"{tmp}:{old_path}"
            started = time.time()
            try:
                with self.assertRaisesRegex(run.Stop, "cost budget"):
                    run.run_agent("opencode", "p", tmp, {"timeout_min": 5, "budget_usd": 3, "opencode_model": "m"})
            finally:
                os.environ["PATH"] = old_path
            self.assertLess(time.time() - started, 15)


class Reconcile(unittest.TestCase):
    def git(self, *args, cwd):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                       capture_output=True)

    def test_merged_worktrees_go_and_closed_issues_lose_status_labels(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.git("init", "-q", "--bare", "-b", "main", str(root / "origin.git"), cwd=tmp)
            checkout = root / "talent-scout-server"
            self.git("clone", "-q", str(root / "origin.git"), str(checkout), cwd=tmp)
            self.git("commit", "-q", "--allow-empty", "-m", "base", cwd=checkout)
            self.git("push", "-q", "origin", "HEAD:main", cwd=checkout)
            for n in (9001, 9002):
                self.git("worktree", "add", "-q", "-b", f"node/{n}", str(root / "wt" / f"server-{n}"), "origin/main",
                         cwd=checkout)
                self.git("commit", "-q", "--allow-empty", "-m", f"work {n}", cwd=root / "wt" / f"server-{n}")
            self.git("push", "-q", "origin", "node/9001:main", cwd=checkout)
            calls = []
            run.ROOT, run.gh = root, lambda *a: calls.append(a) or ""
            nodes = {n.id: n for n in (node(9001, "server", closed=True, labels=["status:review", "needs:human", "type:gap"]),
                                       node(9002, "server", closed=True))}
            run.reconcile(nodes)
            self.assertFalse((root / "wt" / "server-9001").exists())
            self.assertTrue((root / "wt" / "server-9002").exists(), "unmerged work must stay")
            self.assertEqual(calls, [("issue", "edit", "9001", "-R", lib.REPOS["server"],
                                      "--remove-label", "status:review", "--remove-label", "needs:human")])


if __name__ == "__main__":
    unittest.main()
