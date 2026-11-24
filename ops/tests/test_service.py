"""The service (ops/service), dispatch checks (ops/dispatch.py) and the two-step kill switch (ops/stop)."""
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OPS))
import budget
import dispatch
import fakes
import ledger
import lib

HUMAN = "aangwijaya"
CFG = {"budget_usd": 6, "daily_budget_usd": 20, "budget_tz": "Asia/Jakarta", "quota_pause_min": 60,
       "max_attempts": 3, "human_login": HUMAN, "poll_sec": 90}
BLOCK = "```yaml\nfence: [src/*]\nblocked_by: []\nchecks: []\n```\n"
APPROVED = "## Decision\nApproved by the product owner on 2026-11-24.\n\n" + BLOCK


def setUpModule():
    fakes.sandbox_for_tests(tempfile.mkdtemp(prefix="ops-sandbox-"))


def load(name):
    loader = importlib.machinery.SourceFileLoader(name, str(OPS / name))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def make_node(num, repo="client", labels=("run",), body=BLOCK, closed=False):
    agent = lib.AGENTS[repo]
    names = [f"agent:{agent}", *labels]
    return lib.to_node(repo, {"number": num, "title": f"n{num}", "body": body, "state": "CLOSED" if closed else "OPEN",
                              "labels": [{"name": n} for n in names], "comments": []})


class FakeGitHub:
    """Answers the timeline and GraphQL calls dispatch.py makes, and records every other gh call."""

    def __init__(self):
        self.labelled = {}  # (repo, number) -> [actor, ...] in order, for the `run` label
        self.writer = {}  # (repo, number) -> login of the body's last writer
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == "api" and "graphql" in args:
            repo = "client" if any("n=talent-scout-client" == a for a in args) else "server"
            number = int(next(a for a in args if a.startswith("i="))[2:])
            login = self.writer.get((repo, number), HUMAN)
            return json.dumps({"data": {"repository": {"issue": {"author": {"login": HUMAN},
                                                                  "editor": {"login": login}}}}})
        if args[0] == "api":
            path = next(a for a in args if a.startswith("repos/"))
            repo = "client" if "talent-scout-client" in path else "server"
            number = int(path.split("/issues/")[1].split("/")[0])
            events = [{"event": "labeled", "label": {"name": "type:feat"}, "actor": {"login": HUMAN}}]
            for actor in self.labelled.get((repo, number), []):
                events.append({"event": "labeled", "label": {"name": "run"}, "actor": {"login": actor}})
                events.append({"event": "unlabeled", "label": {"name": "run"}, "actor": {"login": actor}})
            if events[-1]["event"] == "unlabeled":
                events.pop()
            # --paginate --slurp gives one list per page
            return json.dumps([events[:1], events[1:]])
        return ""

    def comments(self, node_number=None):
        return [a[-1] for a in self.calls if a[:2] == ("issue", "comment")
                and (node_number is None or a[2] == str(node_number))]

    def removed(self, label):
        return [a[2] for a in self.calls if a[:2] == ("issue", "edit") and "--remove-label" in a
                and a[a.index("--remove-label") + 1] == label]


class ServiceCase(unittest.TestCase):
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

        self.github = FakeGitHub()
        old_gh = dispatch.gh
        dispatch.gh = self.github
        self.addCleanup(lambda: setattr(dispatch, "gh", old_gh))
        service = self.service = load("service")
        run = service.run
        run.gh = self.github
        run.reconcile = lambda nodes, repo=None: None
        self.states = {}
        run.load_state = lambda node_id: self.states.setdefault(node_id, {"attempts": 1, "saws": [], "dead_ends": []})
        run.save_state = lambda node_id, state: self.states.__setitem__(node_id, state)
        run.STOP_FILE = service.STOP_FILE = self.dir / "STOP"
        service.HOLD_FILE = self.dir / "hold"
        service.STATE = self.dir / "state"
        service.sync_contract = lambda: None
        service.remove_boxes = lambda node: []
        self.started = []
        service.start = lambda node, actor: self.started.append((node.id, actor)) or \
            service.children.__setitem__(node.repo, (node.id, FakeChild()))
        self.nodes = {}
        service.load_nodes = lambda ttl=0: self.nodes

    def add(self, *nodes):
        for node in nodes:
            self.nodes[node.id] = node
            if node.has("run"):
                self.github.labelled.setdefault((node.repo, node.number), [HUMAN])

    def events(self, name):
        return [r for _, _, r in ledger.records() if r["event"] == name]


class FakeChild:
    pid, returncode = 4242, None

    def poll(self):
        return None


class Dispatch(ServiceCase):
    def test_the_humans_label_dispatches_the_node(self):
        self.add(make_node(1))
        self.assertEqual(dispatch.check(self.nodes["client#1"], HUMAN), HUMAN)

    def test_a_label_from_anyone_else_is_refused(self):
        self.add(make_node(1))
        self.github.labelled[("client", 1)] = ["ts-agent[bot]"]
        with self.assertRaisesRegex(dispatch.Refused, r"added by ts-agent\[bot\], not aangwijaya"):
            dispatch.check(self.nodes["client#1"], HUMAN)

    def test_only_the_latest_label_counts(self):
        self.add(make_node(1))
        self.github.labelled[("client", 1)] = [HUMAN, "ts-agent[bot]"]
        with self.assertRaises(dispatch.Refused):
            dispatch.check(self.nodes["client#1"], HUMAN)
        self.github.labelled[("client", 1)] = ["ts-agent[bot]", HUMAN]
        self.assertEqual(dispatch.check(self.nodes["client#1"], HUMAN), HUMAN)

    def test_a_contract_change_needs_the_humans_approved_decision(self):
        pending = make_node(2, "server", labels=("run", "contract-change"), body="## Decision\nPending.\n\n" + BLOCK)
        approved = make_node(3, "server", labels=("run", "contract-change"), body=APPROVED)
        self.add(pending, approved)
        with self.assertRaisesRegex(dispatch.Refused, "Approved"):
            dispatch.check(pending, HUMAN)
        self.assertEqual(dispatch.check(approved, HUMAN), HUMAN)
        self.github.writer[("server", 3)] = "ts-orchestrator[bot]"
        with self.assertRaisesRegex(dispatch.Refused, r"last written by ts-orchestrator\[bot\]"):
            dispatch.check(approved, HUMAN)

    def test_the_decision_is_read_from_its_own_section(self):
        self.assertEqual(dispatch.decision("intro Approved\n## Decision\n\nApproved by me.\n"), "Approved by me.")
        self.assertEqual(dispatch.decision("Approved somewhere else"), "")


class Tick(ServiceCase):
    def test_a_ready_node_starts_only_with_the_run_label(self):
        self.add(make_node(1, labels=()), make_node(2))
        self.service.tick(CFG)
        self.assertEqual(self.started, [("client#2", HUMAN)])
        [record] = self.events("dispatched")
        self.assertEqual((record["node"], record["by"]), ("client#2", HUMAN))
        self.assertEqual(self.events("dispatch.refused"), [], "a node nobody dispatched is not even looked at")
        self.assertEqual(self.github.comments(1), [])

    def test_a_refused_label_comes_off_and_the_next_node_starts(self):
        self.add(make_node(1), make_node(2))
        self.github.labelled[("client", 1)] = ["ts-agent[bot]"]
        self.service.tick(CFG)
        self.assertEqual(self.started, [("client#2", HUMAN)])
        self.assertEqual(self.github.removed("run"), ["1"])
        [refused] = self.events("dispatch.refused")
        self.assertEqual(refused["node"], "client#1")
        self.assertTrue(any("Not started" in c for c in self.github.comments(1)))

    def test_one_node_per_repo_and_both_repos_at_once(self):
        self.add(make_node(1), make_node(2), make_node(3, "server"))
        self.service.tick(CFG)
        self.assertEqual(sorted(self.started), [("client#1", HUMAN), ("server#3", HUMAN)])
        self.service.tick(CFG)  # both repos busy: nothing more starts
        self.assertEqual(len(self.started), 2)

    def test_a_human_runner_holding_the_repo_blocks_the_service(self):
        self.add(make_node(1))
        with budget.repo_lock("client"):
            self.service.tick(CFG)
        self.assertEqual(self.started, [])

    def test_hold_and_stop_start_nothing(self):
        self.add(make_node(1))
        for path in (self.service.HOLD_FILE, self.service.STOP_FILE):
            path.touch()
            self.assertEqual(self.service.tick(CFG), "on hold")
            path.unlink()
        self.assertEqual(self.started, [])

    def test_a_paused_agent_waits_and_the_other_repo_runs(self):
        self.add(make_node(1), make_node(2, "server"))
        budget.pause_for_quota("claude", "You've hit your limit", CFG)
        self.service.tick(CFG)
        self.assertEqual(self.started, [("server#2", HUMAN)])

    def test_a_spent_day_starts_nothing(self):
        self.add(make_node(1))
        ledger.append("attempt", actor="orchestrator", node="client#9", cost_usd=19.5)
        self.assertRegex(self.service.tick(CFG), "daily budget spent")
        self.assertEqual(self.started, [])

    def test_the_runner_is_ops_run_for_that_node_unattended(self):
        service = load("service")
        service.STATE = self.dir / "state"
        seen = []

        class Popen:
            pid = 1

            def __init__(self, argv, **kwargs):
                seen.append(argv)
        service.subprocess = type("S", (), {"Popen": Popen, "STDOUT": subprocess.STDOUT})
        service.start(make_node(5), HUMAN)
        self.assertEqual(seen[0][1:], [str(OPS / "run"), "--node", "client#5", "--once", "--auto"])
        self.assertIn("client", service.children)


class Interrupted(ServiceCase):
    def test_an_orphaned_node_runs_again_once(self):
        self.add(make_node(1, labels=("run", "status:in-progress")))
        self.service.tick(CFG)
        node = self.nodes["client#1"]
        self.assertIn("status:todo", node.labels)
        self.assertEqual(self.states["client#1"]["interrupted"], 1)
        [record] = self.events("interrupted")
        self.assertEqual(record["count"], 1)
        self.assertTrue(any("Interrupted" in c for c in self.github.comments(1)))
        self.assertEqual(self.started, [("client#1", HUMAN)], "it keeps its run label and goes again")

    def test_the_second_interruption_goes_to_the_human(self):
        self.add(make_node(1, labels=("run", "status:review")))
        self.states["client#1"] = {"attempts": 2, "saws": [], "dead_ends": [], "interrupted": 1}
        self.service.tick(CFG)
        self.assertEqual(len(self.events("escalated")), 1)
        self.assertEqual(self.github.removed("run"), ["1"])
        self.assertEqual(self.started, [])

    def test_a_node_that_spent_its_attempts_goes_to_the_human(self):
        self.add(make_node(1, labels=("run", "status:in-progress")))
        self.states["client#1"] = {"attempts": 3, "saws": [], "dead_ends": []}
        self.service.tick(CFG)
        self.assertEqual(len(self.events("escalated")), 1)

    def test_a_node_whose_runner_is_alive_is_left_alone(self):
        self.add(make_node(1, labels=("run", "status:in-progress")))
        with budget.repo_lock("client"):
            self.service.tick(CFG)
        self.assertEqual(self.events("interrupted"), [])

    def test_waiting_for_the_human_is_not_an_interruption(self):
        self.add(make_node(1, labels=("status:review", "needs:human")))
        self.service.tick(CFG)
        self.assertEqual(self.events("interrupted"), [])


class HandBack(ServiceCase):
    def test_a_human_stop_takes_the_node_out_of_the_queue(self):
        run = self.service.run
        node = make_node(1)
        run.STOP_FILE.touch()
        run.hand_back(node, run.Stop("stop requested"), CFG)
        [comment] = self.github.comments(1)
        self.assertEqual(lib.parse_handoff(comment)["STATUS"], "needs_human")
        self.assertIn("stopped by the human", comment)
        self.assertEqual(self.github.removed("run"), ["1"])
        self.assertTrue(self.events("handed_back")[0]["by_human"])

    def test_a_paused_node_keeps_its_label_to_go_again_by_itself(self):
        run = self.service.run
        run.hand_back(make_node(1), run.QuotaHit("You've hit your limit"), CFG)
        self.assertEqual(self.github.removed("run"), [])

    def test_ready_and_escalated_nodes_lose_the_run_label(self):
        self.service.run.escalate(make_node(1), "Tier 2 PR is green", event="ready")
        self.assertEqual(self.github.removed("run"), ["1"])


class KillSwitch(ServiceCase):
    def stop(self, *args, boxes=()):
        bin_dir = self.dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        podman = bin_dir / "podman"
        podman.write_text("#!/bin/sh\necho \"$*\" >> \"$(dirname \"$0\")/podman.log\"\n"
                          "[ \"$1\" = ps ] && printf '%s\\n' " + " ".join(boxes or ["''"]) + "\nexit 0\n")
        podman.chmod(podman.stat().st_mode | stat.S_IEXEC)
        code = ("import sys, pathlib; sys.path.insert(0, %r); import budget, container, lib;"
                "container.PODMAN=%r; budget.STATE=pathlib.Path(%r); budget.PAUSE_FILE=budget.STATE/'paused.json';"
                "lib.STOP_FILE=pathlib.Path(%r); lib.HOLD_FILE=pathlib.Path(%r);"
                "import runpy; sys.argv=['stop', *%r]; runpy.run_path(%r, run_name='__main__')"
                % (str(OPS), str(podman), str(budget.STATE), str(self.dir / "STOP"), str(self.dir / "hold"),
                   list(args), str(OPS / "stop")))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
        log = bin_dir / "podman.log"
        return out, log.read_text() if log.exists() else ""

    def test_stop_holds_dispatch_and_kills_nothing(self):
        _, podman = self.stop()
        self.assertTrue((self.dir / "hold").exists())
        self.assertFalse((self.dir / "STOP").exists())
        self.assertNotIn("rm", podman)
        self.assertEqual(len(self.events("stop.hold")), 1)

    def test_stop_now_removes_work_containers_but_not_the_proxy(self):
        out, podman = self.stop("--now", boxes=["ts-claude-client-1-99", "ts-check-abc", "ts-egress"])
        self.assertTrue((self.dir / "STOP").exists())
        [rm] = [l for l in podman.splitlines() if l.startswith("rm")]
        self.assertIn("ts-claude-client-1-99", rm)
        self.assertIn("ts-check-abc", rm)
        self.assertNotIn("ts-egress", rm)
        [record] = self.events("stop.now")
        self.assertEqual(record["actor"], "human")
        self.assertIn("removed 2", out)

    def test_resume_reopens_everything(self):
        self.stop("--now")
        budget.pause_for_quota("claude", "You've hit your limit", CFG)
        self.stop("--resume")
        self.assertFalse((self.dir / "STOP").exists() or (self.dir / "hold").exists())
        self.assertEqual(budget.paused(), {})
        self.assertEqual(len(self.events("stop.resumed")), 1)


class Install(unittest.TestCase):
    def test_the_unit_runs_the_service_and_stops_its_children_with_it(self):
        unit = load("service").unit_text()
        self.assertIn(f"ExecStart=", unit)
        self.assertIn(str(OPS / "service"), unit)
        self.assertIn("KillMode=control-group", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=default.target", unit)


if __name__ == "__main__":
    unittest.main()
