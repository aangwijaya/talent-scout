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
import container
import fakes
import lib
from fakes import git_worktree


FIXTURE_DIR = tempfile.mkdtemp(prefix="ops-tests-")
FAKE_AGENT_TOKEN = "github_pat_" + "x" * 40


def setUpModule():
    # Every agent run and check goes through the sandbox (ADR 0002): here, a podman double that runs
    # the command on the host with only its env file, and clones and proxy files under a temp dir.
    fakes.sandbox_for_tests(tempfile.mkdtemp(prefix="ops-sandbox-"))


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name, str(OPS / name))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    if name == "run":
        # Never let a test reach the real agent token; tests that need it missing set their own path.
        token = Path(FIXTURE_DIR, "agent.gh-token")
        token.write_text(FAKE_AGENT_TOKEN + "\n")
        module.AGENT_GH_TOKEN = token
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


class Fence(unittest.TestCase):
    cfg = {"always_allowed": []}
    block = {"fence": ["src/hooks.server.ts", "src/lib/server/*", "internal/http/*"]}

    def test_the_test_beside_a_fenced_file_is_inside(self):
        files = ["src/hooks.server.test.ts", "src/hooks.server.spec.ts", "src/lib/server/x.test.ts",
                 "internal/http/router_test.go"]
        self.assertEqual(checks.check_fence(files, self.block, self.cfg), [])

    def test_a_test_of_an_unfenced_file_is_still_outside(self):
        for path in ["src/routes/other.test.ts", "src/hooks.client.test.ts", "internal/store/db_test.go",
                     "src/hooks.server.txt"]:
            self.assertTrue(checks.check_fence([path], self.block, self.cfg), path)

    def test_route_params_in_brackets_are_literal(self):
        # SvelteKit routes put params in brackets; fnmatch would read [jobId] as a character class.
        block = {"fence": ["src/routes/jobs/[jobId]/+page.svelte", "src/routes/runs/[runId]/*"]}
        inside = ["src/routes/jobs/[jobId]/+page.svelte", "src/routes/runs/[runId]/+page.server.ts",
                  "src/routes/runs/[runId]/page.server.test.ts"]
        self.assertEqual(checks.check_fence(inside, block, self.cfg), [])
        for path in ["src/routes/jobs/j/+page.svelte", "src/routes/runs/r/+page.svelte"]:
            self.assertTrue(checks.check_fence([path], block, self.cfg), path)

    def test_svelte_component_tests_map_to_the_component(self):
        self.assertEqual(checks.tested_sources("src/a/Card.svelte.test.ts")[:2], ["src/a/Card.svelte.ts", "src/a/Card.svelte"])
        self.assertIn("src/a/Card.svelte", checks.tested_sources("src/a/Card.test.ts"))


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

    def test_findings_commented_on_the_issue_reach_the_brief(self):
        n = node(21, labels=["agent:opencode"], comments=["prune buckets in Allow", f"{lib.HANDOFF_MARK}\nNEXT: handoff-only-once"])
        brief = load_script("brief").build(n)
        self.assertIn("prune buckets in Allow", brief)
        self.assertEqual(brief.count("handoff-only-once"), 1)


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
                    run.run_agent("opencode", "p", git_worktree(tmp), {"timeout_min": 5, "budget_usd": 1, "opencode_model": "fake/model"})
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
                    run.run_agent("opencode", "p", git_worktree(tmp), {"timeout_min": 5, "budget_usd": 1, "opencode_model": "fake/model"})
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
                code, text = run.run_agent("opencode", "p", git_worktree(tmp), {"timeout_min": 1, "budget_usd": 1, "opencode_model": "fake/model"})
            finally:
                os.environ["PATH"] = old_path
            self.assertEqual((code, len(text)), (0, 300000))

    def test_opencode_runs_in_the_worktree_and_may_read_the_go_module_cache(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as wt_root:
            wt = git_worktree(wt_root)
            fake = Path(tmp, "opencode")
            fake.write_text('#!/bin/sh\necho "PWD=$PWD"\necho "ARGS=$*"\necho "CFG=$OPENCODE_CONFIG_CONTENT"\n')
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            old_path = os.environ["PATH"]
            os.environ["PATH"] = f"{tmp}:{old_path}"
            run.STATE, run.HANDOFF_DIR = Path(tmp), Path(tmp, "handoff")
            try:
                _, text = run.run_agent("opencode", "p", wt, {"timeout_min": 1, "budget_usd": 1, "opencode_model": "fake/model", "opencode_effort": "high"})
            finally:
                os.environ["PATH"] = old_path
            # The agent works in its own clone (ADR 0004), never the human's worktree.
            clone = container.clone_path(wt)
            self.assertIn(f"PWD={clone}\n", text)
            self.assertIn(f"--dir {clone}", text)
            self.assertIn("--variant high", text)
            self.assertIn("--format json", text)
            cfg = json.loads(text.split("CFG=", 1)[1])
            allowed = cfg["permission"]["external_directory"]
            # A rejected "ask" ends an OpenCode run; "deny" lets the agent read the error and carry on.
            self.assertEqual(list(allowed.items())[0], ("*", "deny"))
            self.assertEqual({v for k, v in allowed.items() if k != "*"}, {"allow"})
            self.assertIn(f"{tmp}/handoff/*", allowed)
            self.assertNotIn(f"{tmp}/*", allowed)
            self.assertTrue(any(k.endswith("/pkg/mod/**") or k.endswith("/mod/**") for k in allowed))

    def test_claude_uses_the_pinned_model_as_worker_and_reviewer(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as wt_root:
            wt = git_worktree(wt_root)
            fake = Path(tmp, "claude")
            fake.write_text('#!/bin/sh\necho "ARGS=$*"\n')
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            old_path = os.environ["PATH"]
            os.environ["PATH"] = f"{tmp}:{old_path}"
            run.STATE, run.HANDOFF_DIR = Path(tmp), Path(tmp, "handoff")
            cfg = {"timeout_min": 1, "budget_usd": 1, "opencode_model": "m",
                   "claude_model": "claude-test-model", "claude_effort": "low"}
            try:
                _, worker = run.run_agent("claude", "p", wt, cfg)
                _, reviewer = run.run_agent("claude", "p", wt, cfg, read_only=True)
            finally:
                os.environ["PATH"] = old_path
            for args in (worker, reviewer):
                self.assertIn("--model claude-test-model", args)
                self.assertIn("--effort low", args)

    def test_a_prompt_larger_than_the_argument_limit_goes_through_stdin(self):
        run = load_script("run")
        prompt = "MARKER-START\n" + "x" * 200_000 + "\nMARKER-END"  # one argv string is capped at 128 KiB
        for agent in ("claude", "opencode"):
            with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as wt_root:
                wt = git_worktree(wt_root)
                fake = Path(tmp, agent)
                fake.write_text('#!/bin/sh\necho "ARGC=$#"\nprintf "STDIN="; tail -c 10\necho\n')
                fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
                old_path = os.environ["PATH"]
                os.environ["PATH"] = f"{tmp}:{old_path}"
                run.STATE, run.HANDOFF_DIR = Path(tmp), Path(tmp, "handoff")
                cfg = {"timeout_min": 1, "budget_usd": 1, "opencode_model": "m",
                       "claude_model": "c", "claude_effort": "low"}
                try:
                    code, text = run.run_agent(agent, prompt, wt, cfg, read_only=True)
                finally:
                    os.environ["PATH"] = old_path
                self.assertEqual(code, 0, agent)
                self.assertIn("STDIN=MARKER-END", text, agent)
                self.assertNotIn("MARKER", text.split("STDIN=")[0], agent)

    def test_review_findings_are_read_with_or_without_a_fence(self):
        run = load_script("run")
        finding = {"severity": "Low", "file": "a.go", "summary": "x"}
        fenced = 'notes\n```json\n{"findings": [{"severity": "High", "file": "b", "summary": "y"}]}\n```'
        cases = {
            fenced: "High",
            'I traced it.\n{"findings": [' + json.dumps(finding) + "]}": "Low",
            'first {"findings": []} then final {"findings": [' + json.dumps(finding) + "]}": "Low",
        }
        for text, severity in cases.items():
            self.assertEqual(run.parse_findings(text)[0]["severity"], severity)
        self.assertEqual(run.parse_findings('```json\n{"findings": []}\n```'), [])
        self.assertIsNone(run.parse_findings("no verdict here"))

    def test_tiers_pin_a_model_for_each_agent(self):
        cfg = lib.config()
        for key in ("claude_model", "claude_effort", "opencode_model", "opencode_effort"):
            self.assertTrue(cfg.get(key), key)
        self.assertIn(cfg["claude_effort"], ("low", "medium", "high", "xhigh", "max"))
        self.assertIn(cfg["opencode_effort"], ("minimal", "low", "medium", "high", "max"))
        self.assertNotIn(" ", cfg["opencode_model"] + cfg["claude_model"])

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
                code, text = run.run_agent("opencode", "p", git_worktree(tmp), {"timeout_min": 1, "budget_usd": 1, "opencode_model": "m"})
            finally:
                os.environ["PATH"] = old_path
            self.assertEqual((code, text), (0, "done"))
            self.assertIn("→ bash go test ./... [completed]", Path(tmp, "server-9.log").read_text())

    def test_an_opencode_api_error_is_logged_and_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            error = {"type": "error", "error": {"name": "APIError", "data": {"message": "Insufficient Balance", "statusCode": 402}}}
            run = self.fake_opencode(tmp, f"echo '{json.dumps(error)}'\nexit 1\n")
            run.STATE, run.HANDOFF_DIR = Path(tmp), Path(tmp, "handoff")
            old_path = os.environ["PATH"]
            os.environ["PATH"] = f"{tmp}:{old_path}"
            try:
                code, _ = run.run_agent("opencode", "p", git_worktree(tmp), {"timeout_min": 1, "budget_usd": 1, "opencode_model": "m"})
            finally:
                os.environ["PATH"] = old_path
            self.assertEqual(code, 1)
            self.assertEqual(run.LAST_AGENT_ERROR, "opencode APIError 402: Insufficient Balance")
            self.assertIn("ERROR opencode APIError 402: Insufficient Balance", Path(tmp, "server-9.log").read_text())

    def test_a_clean_run_clears_the_last_agent_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = self.fake_opencode(tmp, "echo '" + json.dumps({"type": "text", "part": {"text": "ok"}}) + "'\n")
            run.STATE, run.HANDOFF_DIR = Path(tmp), Path(tmp, "handoff")
            run.LAST_AGENT_ERROR = "opencode APIError 402: old"
            old_path = os.environ["PATH"]
            os.environ["PATH"] = f"{tmp}:{old_path}"
            try:
                run.run_agent("opencode", "p", git_worktree(tmp), {"timeout_min": 1, "budget_usd": 1, "opencode_model": "m"})
            finally:
                os.environ["PATH"] = old_path
            self.assertIsNone(run.LAST_AGENT_ERROR)

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
                    run.run_agent("opencode", "p", git_worktree(tmp), {"timeout_min": 5, "budget_usd": 3, "opencode_model": "m"})
            finally:
                os.environ["PATH"] = old_path
            self.assertLess(time.time() - started, 15)


    def test_opencode_is_stopped_when_it_goes_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            step = json.dumps({"type": "step_start", "part": {}})
            run = self.fake_opencode(tmp, f"echo '{step}'\nsleep 60\n")
            run.STATE, run.HANDOFF_DIR = Path(tmp), Path(tmp, "handoff")
            old_path = os.environ["PATH"]
            os.environ["PATH"] = f"{tmp}:{old_path}"
            started = time.time()
            try:
                with self.assertRaisesRegex(run.Stop, "no output for"):
                    run.run_agent("opencode", "p", git_worktree(tmp), {"timeout_min": 5, "idle_min": 0.05, "budget_usd": 3,
                                                         "opencode_model": "m"})
            finally:
                os.environ["PATH"] = old_path
            self.assertLess(time.time() - started, 15)

    def test_a_stalled_review_is_reported_on_the_pr_instead_of_blocking(self):
        run = load_script("run")
        comments = []

        def stalled(agent, prompt, wt, cfg, read_only=False):
            self.assertEqual(cfg["timeout_min"], 20, "the review runs on its own, shorter budget")
            raise run.Stop("no output for 10 min")

        run.run_agent, run.gh = stalled, lambda *a: comments.append(a) or ""
        run.sh = lambda cmd, cwd=None, check=True: subprocess.CompletedProcess(cmd, 0, "node/1-x\n", "")
        findings = run.independent_review(node(1), "/tmp", {"timeout_min": 60, "review_timeout_min": 20})
        self.assertIsNone(findings)
        self.assertEqual(run.REVIEW_UNAVAILABLE, "no output for 10 min")
        self.assertTrue(any("Independent review unavailable" in str(c) for c in comments))

class ContractSync(unittest.TestCase):
    def run_sync(self, open_pr, *args):
        sync = load_script("contract-sync")
        calls = []

        def fake_sh(cmd, cwd=None, check=True):
            calls.append(cmd)
            out = "abcdef1234567890\n" if cmd[:2] == ["git", "rev-parse"] else ""
            if cmd[:2] == ["git", "show"] and cmd[2].endswith("contract.lock"):
                out = json.dumps({"files": {}})
            return subprocess.CompletedProcess(cmd, 0, out, "")

        sync.sh = fake_sh
        sync.gh = lambda *a: open_pr if a[:2] == ("pr", "list") else ""
        old = sys.argv
        sys.argv = ["contract-sync", *args]
        try:
            return sync.main(), calls
        finally:
            sys.argv = old

    def test_an_open_sync_pr_is_not_drift_and_is_not_opened_twice(self):
        for args in ((), ("--check",)):
            code, calls = self.run_sync("https://github.com/o/c/pull/34\n", *args)
            self.assertEqual(code, 0)
            self.assertFalse(any("worktree" in c for c in calls))

    def test_drift_without_a_sync_pr_is_reported(self):
        code, _ = self.run_sync("", "--check")
        self.assertEqual(code, 1)

    def test_the_sync_pr_regenerates_the_api_types_before_committing(self):
        sync = load_script("contract-sync")
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            def fake_sh(cmd, cwd=None, check=True):
                calls.append(cmd)
                if cmd[:3] == ["git", "worktree", "add"]:
                    for path in sync.FILES.values():
                        Path(cmd[4], path).parent.mkdir(parents=True, exist_ok=True)
                out = "abcdef1234567890\n" if cmd[:2] == ["git", "rev-parse"] else ""
                if cmd[:2] == ["git", "show"] and cmd[2].endswith("contract.lock"):
                    out = json.dumps({"files": {}})
                return subprocess.CompletedProcess(cmd, 0, out, "")

            sync.ROOT, sync.sh, sync.gh = Path(tmp), fake_sh, lambda *a: ""
            old = sys.argv
            sys.argv = ["contract-sync"]
            try:
                self.assertEqual(sync.main(), 0)
            finally:
                sys.argv = old
        # CI fails the PR when schema.d.ts lags openapi.yaml, so the types are regenerated before the commit.
        install = calls.index(["pnpm", "install", "--frozen-lockfile"])
        gen = calls.index(["pnpm", "gen:api"])
        add = calls.index(["git", "add", "-A"])
        self.assertLess(install, gen)
        self.assertLess(gen, add)


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
            run.reconcile(nodes, "client")
            self.assertEqual(calls, [], "a client runner must not touch server items")
            self.assertTrue((root / "wt" / "server-9001").exists())
            run.reconcile(nodes, "server")
            self.assertFalse((root / "wt" / "server-9001").exists())
            self.assertTrue((root / "wt" / "server-9002").exists(), "unmerged work must stay")
            self.assertEqual(calls, [("issue", "edit", "9001", "-R", lib.REPOS["server"],
                                      "--remove-label", "status:review", "--remove-label", "needs:human")])


class CheckEnv(Gate):
    _saved_ensure_clone, _saved_run_code = staticmethod(container.ensure_clone), staticmethod(container.run_code)
    _saved_clone_path = staticmethod(container.clone_path)
    """Sandbox stage 1: commands that run code from a worktree get the toolchain env, never secrets."""
    SECRETS = {"DATABASE_URL": "postgres://u:p@h/db", "OPENROUTER_API_KEY": "sk-or-fake",
               "GITHUB_TOKEN": "ghp_human", "GH_TOKEN": "github_pat_human", "SSH_AUTH_SOCK": "/tmp/agent.sock",
               "AWS_SECRET_ACCESS_KEY": "x", "ANTHROPIC_API_KEY": "sk-ant-x", "CLAUDE_CODE_OAUTH_TOKEN": "x"}

    def setUp(self):
        super().setUp()
        self.saved = dict(os.environ)
        os.environ.update(self.SECRETS)
        self.dump = Path(self.wt).parent / f"env-{os.getpid()}-{id(self)}.txt"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        self.dump.unlink(missing_ok=True)
        for name in ("ensure_clone", "run_code", "clone_path"):  # prepare() tests replace these
            setattr(container, name, getattr(type(self), f"_saved_{name}"))
        super().tearDown()

    def names(self):
        return {line.split("=", 1)[0] for line in self.dump.read_text().splitlines() if "=" in line}

    def assert_clean(self):
        names = self.names()
        for name in self.SECRETS:
            self.assertNotIn(name, names)
        self.assertIn("PATH", names)
        # Inside the sandbox: the translated check env, the warm caches and the shell's own variables.
        allowed = set(container.container_env(lib.check_env())) | set(container.cache_env("server"))
        self.assertLessEqual(names - {"PATH", "PWD", "SHLVL", "_", "OLDPWD"}, allowed)

    def test_green_commands_get_no_secrets(self):
        self.assertEqual(checks.run_commands([f"env > {self.dump}"], self.wt, "green"), [])
        self.assert_clean()

    def test_the_red_check_gets_no_secrets(self):
        self.good_change()
        # The command fails on the base commit, so red passes; it records the env it ran with.
        result = self.run_gate(block=f"fence: [src/*, tests/*]\nchecks: [env > {self.dump}; false]\n")
        self.assertNotIn("red", self.failed(result))
        self.assert_clean()

    def test_check_env_has_no_model_logins(self):
        env = lib.check_env()
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "GH_TOKEN"):
            self.assertNotIn(name, env)
        self.assertEqual(env["PATH"], os.environ["PATH"])

    def prepare(self, repo, number):
        """prepare() with git faked, recording what runs in the sandbox."""
        run = load_script("run")
        sandboxed = []
        root = Path(tempfile.mkdtemp(prefix="prepare-"))

        def fake_sh(cmd, cwd=None, timeout=None, check=True, env=None):
            if cmd[:3] == ["git", "worktree", "add"]:
                Path(cmd[4]).mkdir(parents=True)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def fake_clone(wt):
            clone = root / "clones" / Path(wt).name
            clone.mkdir(parents=True, exist_ok=True)
            return clone

        def fake_run_code(command, cwd, repo, cfg, timeout=None, extra_mounts=()):
            sandboxed.append((command, cwd))
            return subprocess.CompletedProcess(command, 0, "", "")

        run.sh, run.ROOT, run.OPS = fake_sh, root, root / "ops"
        run.container.ensure_clone, run.container.run_code = fake_clone, fake_run_code
        run.container.clone_path = lambda wt: root / "clones" / Path(wt).name
        (root / "ops" / "env").mkdir(parents=True)
        for name in ("client", "server"):
            (root / "ops" / "env" / f"{name}.env.agent").write_text("SECRET=1\n")
        return run.prepare(node(number, repo=repo)), root, sandboxed

    def test_dependency_install_runs_in_the_sandbox_for_worktree_and_clone(self):
        wt, root, sandboxed = self.prepare("client", 7)
        self.assertEqual(sandboxed, [("pnpm install --frozen-lockfile", str(wt)),
                                     ("pnpm install --frozen-lockfile", str(root / "clones" / "client-7"))])

    def test_only_the_client_worktree_and_its_clone_get_an_env_file(self):
        # Nothing the server agent runs reads .env (config and live tests use the process env only), so
        # the server's agent secrets never enter its worktree. The client build needs its local values.
        wt, root, _ = self.prepare("client", 7)
        self.assertTrue((wt / ".env").exists())
        self.assertTrue((root / "clones" / "client-7" / ".env").exists())
        wt, root, sandboxed = self.prepare("server", 8)
        self.assertFalse((wt / ".env").exists())
        self.assertEqual(sandboxed, [])


class AgentEnv(unittest.TestCase):
    """Sandbox stage 0: agents get an allowlisted env and their own GitHub token, never the human's."""
    SECRETS = {"DATABASE_URL": "postgres://u:p@h/db", "OPENROUTER_API_KEY": "sk-or-fake",
               "GITHUB_TOKEN": "ghp_human", "SSH_AUTH_SOCK": "/tmp/agent.sock", "AWS_SECRET_ACCESS_KEY": "x"}

    def setUp(self):
        self.saved = dict(os.environ)
        os.environ.update(self.SECRETS)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_env_drops_everything_outside_the_allowlist(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp:
            env = run.agent_env(tmp, tmp)
        for name in self.SECRETS:
            self.assertNotIn(name, env)
        self.assertEqual(env["PATH"], os.environ["PATH"])
        self.assertEqual(env["PWD"], tmp)
        allowed = set(run.AGENT_ENV_KEEP) | set(run.AGENT_ENV_SET)
        self.assertLessEqual({k for k in env if not k.startswith("GIT_CONFIG_")}, allowed)

    def test_agent_gets_its_own_token_and_no_gh_login(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp:
            env = run.agent_env(tmp, tmp)
            self.assertEqual(env["GH_TOKEN"], FAKE_AGENT_TOKEN)
            # An empty gh config dir: without GH_TOKEN, gh has no login to fall back to.
            self.assertEqual(env["GH_CONFIG_DIR"], tmp)

    def test_git_reaches_github_over_https_with_the_agent_token_only(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp:
            env = run.agent_env(tmp, tmp)
            git = lambda *a: subprocess.run(["git", *a], env=env, cwd=tmp, capture_output=True, text=True).stdout
            self.assertEqual(git("config", "--get-all", "url.https://github.com/.insteadOf").split(),
                             ["git@github.com:", "ssh://git@github.com/"])
            # An empty helper first clears any helper the human configured; then only gh, which reads GH_TOKEN.
            self.assertEqual(git("config", "--get-all", "credential.helper").split("\n")[:2],
                             ["", "!gh auth git-credential"])
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_missing_token_stops_before_the_agent_starts(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp:
            run.AGENT_GH_TOKEN = Path(tmp, "absent")
            with self.assertRaises(run.Stop) as err:
                run.agent_env(tmp, tmp)
            self.assertIn("WORKFLOW.md", str(err.exception))

    def test_a_token_that_is_not_fine_grained_is_refused(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp:
            for wrong in ("ghp_classic", "gho_from_gh_login", ""):
                run.AGENT_GH_TOKEN = Path(tmp, "token")
                run.AGENT_GH_TOKEN.write_text(wrong)
                with self.assertRaises(run.Stop):
                    run.agent_env(tmp, tmp)

    def test_the_agent_process_sees_only_the_agent_env(self):
        run = load_script("run")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as wt_root:
            wt = git_worktree(wt_root)
            fake = Path(tmp, "opencode")
            fake.write_text("#!/bin/sh\nenv\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            os.environ["PATH"] = f"{tmp}:{os.environ['PATH']}"
            run.STATE, run.HANDOFF_DIR = Path(tmp), Path(tmp, "handoff")
            _, text = run.run_agent("opencode", "p", wt, {"timeout_min": 1, "budget_usd": 1, "opencode_model": "fake/model"})
            names = {line.split("=", 1)[0] for line in text.splitlines() if "=" in line}
            for name in self.SECRETS:
                self.assertNotIn(name, names)
            self.assertIn(f"GH_TOKEN={FAKE_AGENT_TOKEN}", text.splitlines())


if __name__ == "__main__":
    unittest.main()
