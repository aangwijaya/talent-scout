---
status: proposed
---

# All agent code runs in a fail-closed sandbox

The sandbox defends against both accidents and prompt injection that tries to exfiltrate data. It wraps the worker and the reviewer, and also every command the orchestrator runs on code from a worktree: green checks, the red check and dependency installs. Before this decision those commands ran on the host with the human's full environment, which was a larger hole than the agent session itself. The lead agent and the verifier stay outside, because they work through Claude Code's own permission harness with the human present.

If the sandbox cannot start, the node stops with a clear error and nothing runs on the host. A `sandbox: host | container` flag exists only for the pilot, and host mode is removed after it.

## Consequences

- Network access is an egress allowlist enforced by an off-the-shelf proxy (tinyproxy or Squid) in its own container on an internal Podman network. Every request, allowed or refused, is logged per node.
  - The allowlist is `api.anthropic.com` (plus Claude login hosts only if token refresh needs them), `opencode.ai`, `api.github.com`, `github.com`, `proxy.golang.org`, `sum.golang.org` and `registry.npmjs.org`.
  - `GOPROXY` has no `direct` fallback.
  - Adding a host means changing the allowlist file, which is tier 3.
- A refused request does not stop the run. The agent gets an ordinary error, and each refusal is listed in the handoff's `DEAD ENDS` and in `ops/triage`, where the human decides whether it was a legitimate need (add the host in a tier 3 PR) or a suspicious attempt.
- Host mode is removed once two nodes per repo have run in the sandbox with all four of these:
  - green checks match CI;
  - `--check-sandbox` passes every session;
  - no egress refusal is left unexplained;
  - run time stays within 1.5× of host mode.
- Each sandbox is capped at 4 CPUs, 6 GB of memory and 1024 processes, on top of the existing 60-minute timeout. The limits live in `ops/tiers.yaml`.
- Claude runs on a long-lived token from `ops/env/claude.oauth-token`. OpenCode gets a minimal copy of its login with a spend-capped key. Both sit in a throwaway home directory per run, so nothing a run writes there persists.
- `ops/run --check-sandbox` proves the walls from inside before the first node of a session:
  - egress is refused to an unlisted host and allowed to a listed one;
  - the human's SSH and gh config are invisible;
  - the main `.git` is not writable;
  - no secrets are in the environment;
  - the memory limit holds.
- Secrets that workers don't use with `LLM_MODE=fake` are removed from `ops/env/*.env.agent` (OpenRouter key, Supabase service-role key, database URL). A test database for agents would be a separate, later decision that adds its host to the allowlist.
- The agent CLIs' logins are mounted minimally and read-only, and model keys used inside the sandbox have spend caps, so a leaked token has a bounded cost.
- Outside the sandbox the orchestrator keeps only `git` (diff, fetch, push) and `gh`, using the human's login.
