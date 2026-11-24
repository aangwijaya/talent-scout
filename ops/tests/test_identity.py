"""GitHub App tokens (ops/identity.py) and the encrypted secret store (ops/creds.py), ADR 0006."""
import base64
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OPS))
import creds
import identity
import lib

# A stand-in for systemd-creds: "encrypts" to the name plus base64, and refuses a wrong name like the real one.
FAKE_CREDS = r'''#!/usr/bin/env python3
import base64, sys
args = [a for a in sys.argv[1:] if a != "--user"]
verb, name, src, dst = args[0], args[1].split("=", 1)[1], args[2], args[3]
data = sys.stdin.buffer.read() if src == "-" else open(src, "rb").read()
if verb == "encrypt":
    out = (name + "\n").encode() + base64.b64encode(data)
else:
    stored_name, body = data.split(b"\n", 1)
    if stored_name.decode() != name:
        sys.exit("Name in credential doesn't match expectations.")
    out = base64.b64decode(body)
(sys.stdout.buffer.write(out) if dst == "-" else open(dst, "wb").write(out))
'''


def b64decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class FakeGitHub(BaseHTTPRequestHandler):
    public_key = None
    requests = []

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def jwt_ok(self):
        token = self.headers["Authorization"].split(" ", 1)[1]
        header, payload, signature = token.split(".")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "sig").write_bytes(b64decode(signature))
            Path(tmp, "pub").write_text(self.public_key)
            done = subprocess.run(["openssl", "dgst", "-sha256", "-verify", f"{tmp}/pub", "-signature", f"{tmp}/sig"],
                                  input=f"{header}.{payload}".encode(), capture_output=True)
        claims = json.loads(b64decode(payload))
        return done.returncode == 0, claims

    def do_GET(self):
        ok, claims = self.jwt_ok()
        self.requests.append(("GET", self.path, claims, None))
        if not ok:
            return self.reply(401, {"message": "bad signature"})
        self.reply(200, {"id": 42})

    def do_POST(self):
        ok, claims = self.jwt_ok()
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(("POST", self.path, claims, body))
        if not ok:
            return self.reply(401, {"message": "bad signature"})
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        self.reply(201, {"token": f"ghs_fake_{len(self.requests)}", "expires_at": expires})


class Store(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        fake = self.tmp / "systemd-creds"
        fake.write_text(FAKE_CREDS)
        fake.chmod(0o755)
        saved_bin, saved_names = creds.SYSTEMD_CREDS, dict(creds.NAMES)
        creds.SYSTEMD_CREDS = str(fake)
        creds.NAMES["claude-oauth-token"] = self.tmp / "legacy" / "claude.oauth-token"
        os.environ["OPS_SECRETS_DIR"] = str(self.tmp / "secrets")

        def restore():
            creds.SYSTEMD_CREDS = saved_bin
            creds.NAMES.clear()
            creds.NAMES.update(saved_names)
            os.environ.pop("OPS_SECRETS_DIR", None)
            identity._cache.clear()
        self.addCleanup(restore)


class Creds(Store):
    def test_round_trip_into_a_private_store(self):
        target = creds.write("claude-oauth-token", b"sk-ant-oat01-secret\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(b"sk-ant", target.read_bytes(), "the stored file is not the plain value")
        self.assertEqual(creds.read("claude-oauth-token", {"secret_store": "creds"}), "sk-ant-oat01-secret\n")

    def test_creds_mode_refuses_a_plain_copy_left_behind(self):
        creds.write("claude-oauth-token", b"x")
        legacy = creds.NAMES["claude-oauth-token"]
        legacy.parent.mkdir()
        legacy.write_text("x")
        with self.assertRaisesRegex(creds.SecretMissing, "still a plain file"):
            creds.read("claude-oauth-token", {"secret_store": "creds"})
        self.assertEqual(creds.read("claude-oauth-token", {"secret_store": "files"}), "x")

    def test_missing_unknown_and_bad_mode(self):
        with self.assertRaisesRegex(creds.SecretMissing, "ops/creds import"):
            creds.read("agent-app-key", {"secret_store": "creds"})
        with self.assertRaisesRegex(creds.SecretMissing, "needs secret_store: creds"):
            creds.read("agent-app-key", {"secret_store": "files"})
        with self.assertRaisesRegex(creds.SecretMissing, "unknown secret"):
            creds.path("aws-key")
        with self.assertRaises(creds.SecretMissing):
            creds.mode({"secret_store": "plain"})

    def test_the_command_line_never_prints_a_value(self):
        source = self.tmp / "token"
        source.write_text("sk-ant-oat01-very-secret")
        env = {**os.environ, "OPS_SYSTEMD_CREDS": creds.SYSTEMD_CREDS}
        cli = lambda *a: subprocess.run([str(OPS / "creds"), *a], env=env, capture_output=True, text=True)
        imported = cli("import", "agent-gh-token", str(source), "--remove")
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertFalse(source.exists(), "--remove deletes the plain file")
        for args in (("list",), ("check",)):
            out = cli(*args)
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertNotIn("very-secret", out.stdout + out.stderr)
        self.assertIn("agent-gh-token: ok (24 bytes)", cli("check").stdout)


class Tokens(Store):
    @classmethod
    def setUpClass(cls):
        cls.keydir = tempfile.mkdtemp()
        key, pub = Path(cls.keydir, "key.pem"), Path(cls.keydir, "pub.pem")
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048",
                        "-out", str(key)], check=True, capture_output=True)
        subprocess.run(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)], check=True,
                       capture_output=True)
        cls.key, FakeGitHub.public_key = key.read_bytes(), pub.read_text()
        cls.server = HTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.keydir, ignore_errors=True)

    def setUp(self):
        super().setUp()
        FakeGitHub.requests = []
        saved = identity.API
        identity.API = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(lambda: setattr(identity, "API", saved))
        creds.write("agent-app-key", self.key)
        creds.write("orchestrator-app-key", self.key)
        self.cfg = {"secret_store": "creds", "github_identity": "apps", "agent_app_id": 111,
                    "orchestrator_app_id": 222}

    def test_a_signed_jwt_buys_a_token_for_one_repo_and_the_roles_permissions(self):
        token = identity.token("agent", ["aangwijaya/talent-scout-client"], self.cfg)
        self.assertTrue(token.startswith("ghs_fake_"))
        (get, path, claims, _), (post, path2, _, body) = FakeGitHub.requests
        self.assertEqual((get, path), ("GET", "/repos/aangwijaya/talent-scout-client/installation"))
        self.assertEqual(claims["iss"], "111")
        self.assertLessEqual(claims["exp"] - claims["iat"], 600, "GitHub refuses JWTs longer than 10 minutes")
        self.assertEqual((post, path2), ("POST", "/app/installations/42/access_tokens"))
        self.assertEqual(body["repositories"], ["talent-scout-client"])
        self.assertEqual(body["permissions"], {"issues": "write", "metadata": "read"})

    def test_tokens_are_cached_until_near_expiry(self):
        repos = list(lib.REPOS.values())
        first = identity.token("orchestrator", repos, self.cfg)
        self.assertEqual(identity.token("orchestrator", repos, self.cfg), first)
        self.assertEqual(len(FakeGitHub.requests), 2)
        self.assertEqual(sorted(FakeGitHub.requests[1][3]["repositories"]),
                         ["talent-scout-client", "talent-scout-server"])

    def test_a_wrong_key_is_refused(self):
        other = Path(self.keydir, "other.pem")
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-out", str(other)], check=True, capture_output=True)
        creds.write("agent-app-key", other.read_bytes())
        with self.assertRaisesRegex(identity.IdentityError, "401"):
            identity.token("agent", ["aangwijaya/talent-scout-server"], self.cfg)

    def test_an_unset_app_id_says_what_to_do(self):
        with self.assertRaisesRegex(identity.IdentityError, "agent_app_id is not set"):
            identity.token("agent", ["o/r"], {**self.cfg, "agent_app_id": 0})


class Permissions(unittest.TestCase):
    def test_neither_app_can_change_ci_or_repo_rules(self):
        for role, perms in identity.PERMISSIONS.items():
            for forbidden in ("workflows", "administration", "secrets", "environments", "members"):
                self.assertNotIn(forbidden, perms, role)

    def test_the_agent_can_only_touch_issues(self):
        self.assertEqual(identity.PERMISSIONS["agent"], {"issues": "write", "metadata": "read"})


class Wiring(unittest.TestCase):
    def setUp(self):
        saved = identity.token
        self.calls = []
        identity.token = lambda role, repos, cfg: self.calls.append((role, repos)) or f"ghs_{role}"
        self.addCleanup(lambda: setattr(identity, "token", saved))

    def test_the_orchestrators_gh_uses_the_app_token_only_in_apps_mode(self):
        self.assertIsNone(lib.gh_env({"github_identity": "legacy"}))
        env = lib.gh_env({"github_identity": "apps"})
        self.assertEqual(env["GH_TOKEN"], "ghs_orchestrator")
        self.assertEqual(self.calls, [("orchestrator", list(lib.REPOS.values()))])

    def test_push_keeps_the_token_out_of_argv(self):
        self.assertEqual(lib.push_argv("o/r", "node/1-x", {"github_identity": "legacy"}),
                         ["git", "push", "-q", "-u", "origin", "HEAD"])
        argv = lib.push_argv("o/r", "node/1-x", {"github_identity": "apps"})
        self.assertIn("https://github.com/o/r.git", argv)
        self.assertIn("HEAD:refs/heads/node/1-x", argv)
        self.assertFalse(any("ghs_" in a for a in argv))

    def test_agents_get_a_token_for_their_own_repo_only(self):
        loader = importlib.machinery.SourceFileLoader("run", str(OPS / "run"))
        spec = importlib.util.spec_from_loader("run", loader)
        run = importlib.util.module_from_spec(spec)
        loader.exec_module(run)
        run.config = lambda: {"github_identity": "apps"}
        with tempfile.TemporaryDirectory() as tmp:
            env = run.agent_env(Path(tmp, "server-99"), tmp)
        self.assertEqual(env["GH_TOKEN"], "ghs_agent")
        self.assertEqual(self.calls, [("agent", [lib.REPOS["server"]])])


@unittest.skipUnless(os.environ.get("OPS_REAL_CREDS") == "1", "set OPS_REAL_CREDS=1 to use the host's systemd-creds")
class RealSystemdCreds(unittest.TestCase):
    def test_round_trip_with_the_real_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["OPS_SECRETS_DIR"] = tmp
            saved = creds.SYSTEMD_CREDS
            creds.SYSTEMD_CREDS = "systemd-creds"
            try:
                creds.write("agent-app-key", b"-----BEGIN PRIVATE KEY-----\nabc\n")
                self.assertEqual(creds.read("agent-app-key", {"secret_store": "creds"}),
                                 "-----BEGIN PRIVATE KEY-----\nabc\n")
            finally:
                creds.SYSTEMD_CREDS = saved
                os.environ.pop("OPS_SECRETS_DIR", None)


if __name__ == "__main__":
    unittest.main()
