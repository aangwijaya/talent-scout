"""Sandbox stage 2. Run with: python3 -m unittest discover -s ops/tests

Unit tests use a fake podman. Set OPS_SANDBOX_IT=1 to also run the probe in the real images.
"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OPS))
import container
import fakes
import lib

# Tests never write the real proxy config or logs under ops/.state.
container.EGRESS_DIR = Path(tempfile.mkdtemp(prefix="egress-test-"))

CFG = {"sandbox": "container", "sandbox_cpus": 4, "sandbox_memory": "6g", "sandbox_pids": 1024}


def fake_podman(tmp, image_exists=True):
    missing = () if image_exists else tuple(container.image_tag(r) for r in ("client", "server", "egress"))
    return fakes.fake_podman(tmp, missing=missing)


class Mode(unittest.TestCase):
    def test_container_is_the_only_mode(self):
        self.assertEqual(container.mode({}), "container")
        with self.assertRaises(container.SandboxError) as err:
            container.mode({"sandbox": "host"})
        self.assertIn("removed after the pilot", str(err.exception))

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(container.SandboxError):
            container.mode({"sandbox": "maybe"})


class Image(unittest.TestCase):
    def test_tag_follows_the_containerfile(self):
        tag = container.image_tag("server")
        self.assertRegex(tag, r"^localhost/talent-scout-server:[0-9a-f]{12}$")
        self.assertNotEqual(tag.split(":")[1], container.image_tag("client").split(":")[1])

    def test_a_missing_image_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            container.PODMAN, _ = fake_podman(tmp, image_exists=False)
            with self.assertRaises(container.SandboxError) as err:
                container.require_image("server")
            self.assertIn("ops/sandbox build server", str(err.exception))


class Argv(unittest.TestCase):
    def argv(self, **kw):
        with tempfile.TemporaryDirectory() as wt:
            return container.podman_argv("server", CFG, wt, ["true"], "/tmp/env", **kw), wt

    def test_limits_user_and_privileges(self):
        argv, _ = self.argv()
        for flag in ("--rm", "--init", "--userns=keep-id", "--cpus=4", "--memory=6g", "--pids-limit=1024",
                     "--cap-drop=all", "--security-opt=no-new-privileges"):
            self.assertIn(flag, argv)

    def test_secrets_travel_in_an_env_file_never_on_argv(self):
        argv, _ = self.argv()
        self.assertIn("--env-file", argv)
        self.assertNotIn("-e", argv)

    def test_the_host_home_is_never_mounted_whole(self):
        argv, _ = self.argv(mounts=[(str(Path.home() / "go/pkg/mod"), "O")])
        sources = [argv[i + 1].split(":")[0] for i, a in enumerate(argv) if a == "-v"]
        self.assertNotIn(str(Path.home()), sources)
        self.assertNotIn(str(lib.ROOT), sources)

    def test_a_reviewer_gets_a_discarding_overlay_never_a_writable_mount(self):
        argv, wt = self.argv(read_only=True)
        self.assertIn(f"{wt}:{wt}:O", argv)
        argv, wt = self.argv()
        self.assertIn(f"{wt}:{wt}:rw", argv)


class Env(unittest.TestCase):
    def test_host_paths_and_logins_are_replaced(self):
        env = container.container_env({"PATH": "/snap/bin", "HOME": "/home/agni", "GOCACHE": "/h/c",
                                       "CLAUDE_CONFIG_DIR": "/h/.claude", "GH_TOKEN": "github_pat_x",
                                       "LANG": "C.UTF-8"})
        self.assertNotIn("PATH", env)
        self.assertEqual(env["HOME"], container.HOME)
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)
        self.assertEqual(env["GH_TOKEN"], "github_pat_x")
        self.assertTrue(env["GOCACHE"].startswith("/tmp/"))

    def test_writable_state_lives_outside_the_mounted_login_parents(self):
        # Podman creates HOME/.config and HOME/.local as root to mount the logins under them; an agent
        # CLI that writes state there dies with EACCES before it starts (pilot server#92).
        env = container.container_env({})
        for name in ("XDG_STATE_HOME", "XDG_CACHE_HOME", "GH_CONFIG_DIR"):
            self.assertTrue(env[name].startswith("/tmp/"), name)

    def test_go_never_fetches_outside_the_module_proxy(self):
        env = container.container_env({"GOPROXY": "https://proxy.golang.org,direct"})
        self.assertEqual(env["GOPROXY"], "https://proxy.golang.org")


class Egress(unittest.TestCase):
    """Sandbox stage 3: an internal network whose only way out is the allowlist proxy."""

    def test_every_sandbox_joins_only_the_internal_network(self):
        with tempfile.TemporaryDirectory() as wt:
            argv = container.podman_argv("server", CFG, wt, ["true"], "/tmp/env")
        self.assertIn(f"--network={container.NETWORK}", argv)
        self.assertEqual(sum(a.startswith("--network") for a in argv), 1)

    def test_tools_go_through_the_proxy_and_localhost_stays_local(self):
        env = container.container_env({"HTTPS_PROXY": "http://host-proxy:3128", "no_proxy": "*"})
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertEqual(env[name], container.PROXY_URL)
        for name in ("NO_PROXY", "no_proxy"):
            self.assertEqual(env[name], "localhost,127.0.0.1,::1")

    def test_the_filter_is_exact_anchored_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            allow = Path(tmp, "allow")
            allow.write_text("# comment\napi.github.com\n\nproxy.golang.org  # Go\n")
            self.assertEqual(container.filter_lines(allow), [r"^api\.github\.com$", r"^proxy\.golang\.org$"])

    def test_refused_hosts_are_read_from_the_proxy_log(self):
        log = ('NOTICE  Nov 22 10:00:01 [7]: Proxying refused on filtered domain "example.com"\n'
               'CONNECT Nov 22 10:00:02 [7]: Connect (file descriptor 6): 10.89.0.5\n'
               'NOTICE  Nov 22 10:00:03 [7]: Proxying refused on filtered domain "evil.test"\n'
               'NOTICE  Nov 22 10:00:04 [7]: Proxying refused on filtered domain "example.com"\n')
        self.assertEqual(container.refused_hosts(log), ["example.com", "evil.test"])

    def test_allowed_requests_are_logged_too(self):
        self.assertIn("LogLevel Info", container.tinyproxy_conf())
        log = ('INFO x [7]: Established connection to host "api.anthropic.com" using file descriptor 7.\n'
               'INFO x [8]: Established connection to host "api.github.com" using file descriptor 8.\n'
               'INFO x [9]: Established connection to host "api.anthropic.com" using file descriptor 9.\n')
        self.assertEqual(container.allowed_hosts(log), ["api.anthropic.com", "api.github.com"])

    def test_no_proxy_image_means_nothing_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "podman")
            script.write_text(f"""#!/bin/sh
echo "$@" >> {tmp}/log
case "$*" in "image exists {container.image_tag('egress')}") exit 1 ;; "image exists"*) exit 0 ;; esac
exit 0
""")
            script.chmod(0o755)
            container.PODMAN = str(script)
            with tempfile.TemporaryDirectory() as wt:
                with self.assertRaises(container.SandboxError) as err:
                    lib.run_code(f"touch {wt}/ran", wt, "server", CFG)
                self.assertFalse(Path(wt, "ran").exists())
            self.assertIn("ops/sandbox build egress", str(err.exception))


class RunCode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        container.PODMAN, self.log = fake_podman(self.tmp.name)
        self.saved = dict(os.environ)
        os.environ["OPENROUTER_API_KEY"] = "sk-or-fake"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def test_container_mode_runs_checks_through_podman(self):
        with tempfile.TemporaryDirectory() as wt:
            done = lib.run_code(f"env > {wt}/env.txt", wt, "server", CFG)
            self.assertEqual(done.returncode, 0)
            self.assertIn("run ", self.log.read_text())
            names = {l.split("=", 1)[0] for l in Path(wt, "env.txt").read_text().splitlines()}
        self.assertNotIn("OPENROUTER_API_KEY", names)

    def test_a_leftover_host_setting_runs_nothing(self):
        with tempfile.TemporaryDirectory() as wt:
            with self.assertRaises(container.SandboxError):
                lib.run_code(f"touch {wt}/ran", wt, "server", {"sandbox": "host"})
            self.assertFalse(Path(wt, "ran").exists())

    def test_container_mode_without_an_image_runs_nothing(self):
        container.PODMAN, self.log = fake_podman(self.tmp.name, image_exists=False)
        with tempfile.TemporaryDirectory() as wt:
            with self.assertRaises(container.SandboxError):
                lib.run_code(f"touch {wt}/ran", wt, "server", CFG)
            self.assertFalse(Path(wt, "ran").exists(), "must not fall back to the host")



class WarmCache(unittest.TestCase):
    """ADR 0005: the warm caches are mounted and the tools are pointed at them, or they are never used."""

    def test_go_is_pointed_at_the_mounted_module_cache(self):
        caches = container.warm_caches("server")
        if not caches:
            self.skipTest("no Go module cache on this host")
        env = dict((name, value) for name, value, _ in caches)
        mounts = [path for _, _, path in caches]
        self.assertIn("GOMODCACHE", env)
        self.assertTrue(any(env["GOMODCACHE"] == m or env["GOMODCACHE"].startswith(m + "/") for m in mounts))

    def test_the_build_cache_is_the_sandbox_one_never_the_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = container.GO_BUILD_CACHE
            container.GO_BUILD_CACHE = Path(tmp, "go-build")
            try:
                self.assertNotIn("GOCACHE", container.cache_env("server"), "the host cache misses in the image")
                container.GO_BUILD_CACHE.mkdir()
                self.assertEqual(container.cache_env("server")["GOCACHE"], str(container.GO_BUILD_CACHE))
            finally:
                container.GO_BUILD_CACHE = saved

    def test_pnpm_is_pointed_at_the_mounted_store(self):
        caches = container.warm_caches("client")
        if not caches:
            self.skipTest("no pnpm store on this host")
        env = dict((name, value) for name, value, _ in caches)
        self.assertIn("npm_config_store_dir", env)
        mount = caches[0][2]
        self.assertTrue(env["npm_config_store_dir"] == mount)

    def test_checks_run_with_the_cache_env(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        container.PODMAN, _ = fake_podman(tmp.name)
        if not container.warm_caches("server"):
            self.skipTest("no Go module cache on this host")
        with tempfile.TemporaryDirectory() as wt:
            lib.run_code(f"env > {wt}/env.txt", wt, "server", CFG)
            env = dict(l.split("=", 1) for l in Path(wt, "env.txt").read_text().splitlines() if "=" in l)
        self.assertEqual(env.get("GOMODCACHE"), dict((n, v) for n, v, _ in container.warm_caches("server"))["GOMODCACHE"])


def git_worktree(root, name="server-9"):
    return fakes.git_worktree(root, name)


class AgentInBox(unittest.TestCase):
    """In container mode the agent CLI runs through podman with only the env file."""

    def setUp(self):
        from test_ops import load_script, FAKE_AGENT_TOKEN
        self.token = FAKE_AGENT_TOKEN
        self.tmp = tempfile.TemporaryDirectory()
        container.PODMAN, self.log = fake_podman(self.tmp.name)
        self.run = load_script("run")
        bin_dir = Path(self.tmp.name, "bin")
        bin_dir.mkdir()
        for cli in ("opencode", "claude"):
            fake = bin_dir / cli
            fake.write_text("#!/bin/sh\nenv\n")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.saved = dict(os.environ)
        os.environ.update(PATH=f"{bin_dir}:{os.environ['PATH']}", OPENROUTER_API_KEY="sk-or-fake",
                          DATABASE_URL="postgres://u:p@h/db")
        self.run.STATE, self.run.HANDOFF_DIR = Path(self.tmp.name), Path(self.tmp.name, "handoff")
        self.saved_egress_dir = container.EGRESS_DIR
        container.EGRESS_DIR = Path(self.tmp.name, "egress")  # never the real ops/.state
        self.cfg = {**CFG, "timeout_min": 1, "budget_usd": 1, "opencode_model": "fake/model",
                    "claude_model": "m", "claude_effort": "low"}

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)
        container.EGRESS_DIR = self.saved_egress_dir
        self.tmp.cleanup()

    def wt(self):
        return git_worktree(self.tmp.name)

    def test_the_agent_sees_only_the_sandbox_env(self):
        _, text = self.run.run_agent("opencode", "p", self.wt(), self.cfg)
        env = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        self.assertNotIn("OPENROUTER_API_KEY", env)
        self.assertNotIn("DATABASE_URL", env)
        self.assertEqual(env["HOME"], container.HOME)
        self.assertEqual(env["GH_TOKEN"], self.token)
        self.assertIn("--name", self.log.read_text())

    def test_the_agent_works_in_its_clone_never_the_humans_worktree(self):
        wt = self.wt()
        self.run.run_agent("opencode", "p", wt, self.cfg)
        argv = [line for line in self.log.read_text().splitlines() if line.startswith("run ") and "opencode" in line][0]
        self.assertIn(f"-w {container.clone_path(wt)}", argv)
        self.assertNotIn(f"{wt}:", argv)
        self.assertNotIn("talent-scout-server/.git", argv)

    def test_a_missing_image_stops_before_the_agent_starts(self):
        container.PODMAN, self.log = fake_podman(self.tmp.name, image_exists=False)
        with self.assertRaises(container.SandboxError):
            self.run.run_agent("opencode", "p", self.wt(), self.cfg)
        self.assertNotIn("run ", self.log.read_text())

    def test_claude_needs_its_long_lived_token(self):
        container.CLAUDE_TOKEN = Path(self.tmp.name, "absent")
        with self.assertRaises(container.SandboxError) as err:
            self.run.run_agent("claude", "p", self.wt(), self.cfg)
        self.assertIn("claude setup-token", str(err.exception))


class EgressSurfacing(unittest.TestCase):
    """Stage 3b: a refused host reaches the retry, the handoff and ops/triage."""

    def test_the_egress_line_ends_the_previous_handoff_field(self):
        text = lib.with_egress("STATUS: done\nNEVER: push", ["example.com", "evil.test"])
        fields = lib.parse_handoff(text)
        self.assertEqual(fields["NEVER"], "push")
        self.assertIn("example.com, evil.test", fields["EGRESS"])
        self.assertIn("ops/egress-allowlist", fields["EGRESS"])
        self.assertEqual(lib.with_egress("STATUS: done", []), "STATUS: done")

    def test_refusals_from_state_files_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "client-85.json").write_text('{"attempts": 1, "egress_refused": ["cdn.example"]}')
            Path(tmp, "server-9.json").write_text('{"attempts": 1}')
            self.assertEqual(lib.egress_refusals(Path(tmp)), {"client#85": ["cdn.example"]})

    def test_a_boxed_agent_run_reports_its_refused_hosts(self):
        from test_ops import load_script
        with tempfile.TemporaryDirectory() as tmp:
            container.PODMAN, _ = fake_podman(tmp)
            Path(tmp, "proxy.log").write_text('NOTICE x [1]: Proxying refused on filtered domain "cdn.example"\n')
            run = load_script("run")
            bin_dir = Path(tmp, "bin")
            bin_dir.mkdir()
            Path(bin_dir, "opencode").write_text("#!/bin/sh\necho ok\n")
            Path(bin_dir, "opencode").chmod(0o755)
            saved = dict(os.environ)
            os.environ["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
            try:
                run.STATE, run.HANDOFF_DIR = Path(tmp), Path(tmp, "handoff")
                wt = git_worktree(tmp)
                run.run_agent("opencode", "p", wt, {**CFG, "timeout_min": 1, "budget_usd": 1, "opencode_model": "m"})
            finally:
                os.environ.clear()
                os.environ.update(saved)
            self.assertEqual(run.LAST_EGRESS_REFUSED, ["cdn.example"])

    def test_the_posted_handoff_carries_the_refusals(self):
        from test_ops import load_script, node
        run = load_script("run")
        posted = []
        run.gh = lambda *args: posted.append(args) or ""
        run.load_state = lambda node_id: {"egress_refused": ["cdn.example"]}
        run.post_handoff(node(85), "STATUS: done\nNEVER: x")
        self.assertIn("EGRESS: refused cdn.example", posted[0][-1])


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class AgentClone(unittest.TestCase):
    """Stage 4 (ADR 0004): the agent commits in its own clone; its commits come back as a bundle."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.checkout, self.wt = root / "talent-scout-server", root / "wt" / "server-9"
        self.checkout.mkdir()
        git(self.checkout, "init", "-q", "-b", "main")
        git(self.checkout, "config", "user.email", "h@example.com")
        git(self.checkout, "config", "user.name", "human")
        (self.checkout / "a.txt").write_text("base\n")
        git(self.checkout, "add", "-A")
        git(self.checkout, "commit", "-qm", "base")
        git(self.checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.checkout, "worktree", "add", "-q", str(self.wt), "-b", "node/9-x", "--no-track", "origin/main")
        self.saved = container.CLONES, container.PODMAN
        container.CLONES = root / "clones"
        container.PODMAN, self.log = fake_podman(self.tmp.name)

    def tearDown(self):
        container.CLONES, container.PODMAN = self.saved
        self.tmp.cleanup()

    def test_the_clone_is_a_standalone_repository_on_the_node_branch(self):
        clone = container.ensure_clone(self.wt)
        self.assertTrue((clone / ".git").is_dir(), "a worktree's .git file would point at the human's repo")
        self.assertEqual(git(clone, "branch", "--show-current"), "node/9-x")
        self.assertEqual(git(clone, "rev-parse", "HEAD"), git(self.wt, "rev-parse", "HEAD"))

    def test_agent_mounts_hold_the_clone_and_never_the_humans_git(self):
        clone = container.ensure_clone(self.wt)
        with tempfile.TemporaryDirectory() as home:
            specs = " ".join(str(src) for src, _ in container.agent_mounts(clone, False, Path(self.tmp.name, "h"), home))
        self.assertNotIn(str(self.checkout / ".git"), specs)
        self.assertNotIn(str(self.wt), specs)

    def test_agent_commits_come_back_without_running_any_hook(self):
        clone = container.ensure_clone(self.wt)
        (clone / "a.txt").write_text("agent\n")
        git(clone, "-c", "user.email=a@example.com", "-c", "user.name=agent", "commit", "-qam", "agent work")
        marker = Path(self.tmp.name, "hook-ran")
        hook = self.checkout / ".git" / "hooks" / "reference-transaction"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(0o755)
        container.import_agent_commits(clone, self.wt, "server", CFG)
        self.assertEqual(git(self.wt, "rev-parse", "HEAD"), git(clone, "rev-parse", "HEAD"))
        self.assertEqual((self.wt / "a.txt").read_text(), "agent\n")
        self.assertFalse(marker.exists(), "a hook in the human's repo ran during the import")

    def test_checks_never_mount_the_humans_git(self):
        with tempfile.TemporaryDirectory() as wt:
            lib.run_code("true", str(self.wt), "server", CFG)
        self.assertNotIn(str(self.checkout / ".git"), self.log.read_text())
        self.assertIn("-buildvcs=false", container.container_env({})["GOFLAGS"])


@unittest.skipUnless(os.environ.get("OPS_SANDBOX_IT") == "1", "set OPS_SANDBOX_IT=1 to run against real images")
class Probe(unittest.TestCase):
    def test_every_wall_holds_in_both_images(self):
        container.PODMAN = "podman"
        for repo in ("client", "server"):
            lines = container.probe(repo, CFG)
            self.assertFalse([l for l in lines if l.startswith("FAIL")], "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
