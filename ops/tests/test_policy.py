"""Policy tests: the rules in ops/tiers.yaml and ops/egress-allowlist, checked on every PR.

These test the real files, not fixtures. A change that loosens a rule past these bounds fails CI,
so loosening it means changing this file too, in the same reviewed PR.
"""
import ipaddress
import re
import sys
import unittest
from pathlib import Path

OPS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OPS))
import container
import lib

# Paths whose change is hard to undo or changes what agents may do. Each must stay tier 3.
MUST_BE_TIER3 = [
    # contract, in both repos
    "docs/API.md", "docs/EVENTS.md", "docs/PRD.md", "api/openapi.yaml", "contract.lock",
    # auth, sessions, migrations, deploy
    "src/hooks.server.ts", "src/lib/server/session.ts", "src/lib/server/guards.ts",
    "internal/store/migrations/00009_x.sql", "internal/auth/token.go", "internal/http/middleware/auth.go",
    "cmd/migrate/main.go", "Dockerfile", "vercel.json",
    # CI and the agents' own rules
    ".github/workflows/ci.yml", "CLAUDE.md", "AGENTS.md", ".claude/settings.json",
    ".opencode/agent.md", "opencode.json",
]
# Product code: never tier 1, whatever the tier 1 globs become.
CODE = ["src/routes/+page.svelte", "src/lib/stores/chat.ts", "internal/graph/scorer.go",
        "internal/http/handlers/session.go", "cmd/server/main.go", "config/models.yaml"]

HOST = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

# Safe ranges for the sandbox and run limits.
LIMITS = {"sandbox_cpus": (1, 8), "sandbox_pids": (64, 4096), "budget_usd": (1, 10),
          "timeout_min": (10, 120), "review_timeout_min": (5, 60), "idle_min": (2, 30),
          "max_attempts": (1, 5), "ci_wait_min": (5, 60)}
MAX_MEMORY = 8 * 1024 ** 3


class TierPolicy(unittest.TestCase):
    cfg = lib.config()

    def test_sensitive_paths_stay_tier3(self):
        for path in MUST_BE_TIER3:
            self.assertEqual(lib.tier_of([path], self.cfg), 3, path)

    def test_product_code_is_never_tier1(self):
        for path in CODE:
            self.assertNotEqual(lib.tier_of([path], self.cfg), 1, path)

    def test_automerge_never_includes_tier3(self):
        allowed = {str(t) for t in self.cfg.get("automerge", [])}
        self.assertLessEqual(allowed, {"1", "2"}, f"automerge: {sorted(allowed)}")

    def test_always_allowed_never_opens_a_tier3_path(self):
        for path in self.cfg["always_allowed"]:
            self.assertNotEqual(lib.tier_of([path], self.cfg), 3, path)
            self.assertFalse(any(ch in path for ch in "*?["), f"always_allowed must list exact files: {path}")

    def test_limits_stay_in_safe_ranges(self):
        for key, (low, high) in LIMITS.items():
            value = self.cfg.get(key)
            self.assertIsInstance(value, int, key)
            self.assertTrue(low <= value <= high, f"{key}={value} outside {low}..{high}")
        memory = container.parse_bytes(self.cfg["sandbox_memory"])
        self.assertTrue(256 * 1024 ** 2 <= memory <= MAX_MEMORY, f"sandbox_memory={self.cfg['sandbox_memory']}")


class EgressPolicy(unittest.TestCase):
    hosts = [line.split("#", 1)[0].strip() for line in container.ALLOWLIST.read_text().splitlines()]
    hosts = [h for h in hosts if h]

    def test_every_entry_is_one_exact_hostname(self):
        for host in self.hosts:
            self.assertRegex(host, HOST, f"not an exact lowercase hostname: {host!r}")

    def test_no_ip_addresses(self):
        for host in self.hosts:
            with self.assertRaises(ValueError, msg=host):
                ipaddress.ip_address(host)

    def test_no_duplicates(self):
        self.assertEqual(len(self.hosts), len(set(self.hosts)))

    def test_filter_matches_only_the_listed_host(self):
        # The proxy's filter is anchored, so an allowed host can't be used as a prefix or suffix.
        for pattern, host in zip(container.filter_lines(), self.hosts):
            self.assertRegex(host, pattern)
            self.assertNotRegex(f"evil.{host}", pattern)
            self.assertNotRegex(f"{host}.evil.com", pattern)


if __name__ == "__main__":
    unittest.main()
