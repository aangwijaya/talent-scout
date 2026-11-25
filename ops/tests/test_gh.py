"""gh tries again through GitHub's bad minutes, and only through those."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lib


class Retry(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.answers = []
        patcher = mock.patch.object(lib, "sh", side_effect=self.fake_sh)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeps = mock.patch.object(lib.time, "sleep")
        self.sleep = sleeps.start()
        self.addCleanup(sleeps.stop)
        env = mock.patch.object(lib, "gh_env", return_value={})
        env.start()
        self.addCleanup(env.stop)

    def fake_sh(self, cmd, env=None):
        self.calls.append(cmd)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return mock.Mock(stdout=answer)

    def test_a_graphql_hiccup_is_tried_again(self):
        self.answers = [RuntimeError("['gh', 'issue', 'edit']: GraphQL: Something went wrong while executing your "
                                     "query on 2026-11-25T15:44:13Z."), "ok"]
        self.assertEqual(lib.gh("issue", "edit", "1"), "ok")
        self.assertEqual(len(self.calls), 2)

    def test_a_bad_gateway_is_tried_again(self):
        self.answers = [RuntimeError("non-200 OK status code: 502 Bad Gateway"),
                        RuntimeError("HTTP 503: Service Unavailable"), "ok"]
        self.assertEqual(lib.gh("api", "x"), "ok")
        self.assertEqual(len(self.calls), 3)

    def test_it_gives_up_after_the_last_delay(self):
        self.answers = [RuntimeError("502 Bad Gateway")] * (len(lib.GH_RETRY_DELAYS) + 1)
        with self.assertRaises(RuntimeError):
            lib.gh("api", "x")
        self.assertEqual(len(self.calls), len(lib.GH_RETRY_DELAYS) + 1)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], list(lib.GH_RETRY_DELAYS))

    def test_a_real_error_fails_at_once(self):
        for text in ("HTTP 404: Not Found", "HTTP 403: Resource not accessible by integration",
                     "could not add label: 'run' not found"):
            self.calls.clear()
            self.answers = [RuntimeError(text)]
            with self.assertRaises(RuntimeError, msg=text):
                lib.gh("issue", "edit", "1")
            self.assertEqual(len(self.calls), 1, text)


if __name__ == "__main__":
    unittest.main()
