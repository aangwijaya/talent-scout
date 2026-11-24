# WORKFLOW: Talent Scout agent graph

The only source for how work runs here. To set up the same architecture in another project, start from `BLUEPRINT.md`. App repos: `aangwijaya/talent-scout-client` (Claude) and `aangwijaya/talent-scout-server` (OpenCode). Design basis: harness says what an agent can do, the loop says what is true, the graph says what happens next.

## Daily commands

```bash
ops/next                     # what can run now, computed from GitHub
ops/triage                   # everything waiting on you, in one list
ops/run --repo client        # supervised: asks before each outward step
ops/run --repo server --auto # unattended; run one process per repo for parallel work
ops/run --node client#15     # one node only
ops/auto client#15           # reconcile, then that node with --once --auto in a detached tmux session
ops/run --reconcile          # after you merge: drop stale labels, remove merged worktrees
ops/brief client#15          # the exact context a session receives
ops/stop                     # hand work back and halt; ops/stop --clear re-enables and ends a quota pause
ops/report                   # per-node results from the audit ledger, and the tier 1 auto-merge targets
ops/report --verify          # check the ledger chain and every head published on GitHub
ops/report --note client#12 intervention "fixed the lint by hand" --by lead
                             # record what a person did (intervention or escaped-high)
```

## The node

A node is one GitHub issue with a block in its body:

```yaml
fence: [src/routes/apply/*, src/lib/server/api/*]
blocked_by: [client#25]
checks: [pnpm test]
```

- `fence`: the paths this node may change. The test file beside a fenced file is inside too (`a.test.ts` or `a.spec.ts` for `a.ts` or `a.svelte`, `a_test.go` for `a.go`). Don't use `[` or `]` in globs, because they mean a character class. `*` also matches `/`.
- `blocked_by`: real dependencies. Test each arrow: what would break if B started before A finished? If nothing, delete it.
- `checks`: extra commands that prove this node. `red: false` opts out of the red check, for work with no behavior to test first.

An issue without a block is reported as unmapped and never runs. `day:N` labels no longer decide order. Gaps run first, then the node that unblocks the most others.

## One run of a node

1. **SCOPE**: confirm that dependencies really exist. A gap found here costs one issue, not a broken PR.
2. **RED**: write the checks and watch them fail.
3. **GREEN**: implement.
4. **HANDOFF**: write the structured file. The runner posts it to the issue.
5. **GATE**: `ops/gate` checks the fence, contract, secrets, handoff, green commands, and red proof.
6. **PR and CI**: only the runner pushes, opens PRs, and merges. Agents are denied these commands.
7. **REVIEW**: for tier 2 and 3, a fresh session of the other agent sees only the issue and the diff. Its findings are posted on the PR.
8. **MERGE or ESCALATE**, by tier. When a PR waits for you, the runner prints its link.

On failure the agent gets `EXPECTED / SAW / FENCE / DEAD ENDS` and retries. If the same `SAW` returns twice, or the attempt budget is spent, the node goes to `needs:human`.

Models are pinned in `ops/tiers.yaml`: `claude_model` and `claude_effort` for Claude, `opencode_model` and `opencode_effort` (sent as `--variant`) for OpenCode (`opencode models --verbose` lists the ids). They apply to both the worker and the reviewer role. Each agent call is limited by `timeout_min` and by its share of the budget (below): OpenCode is stopped when its cost events pass it, and Claude enforces it itself. Outside the worktree, agents may only write `ops/.state/handoff/` with their edit tools and read the Go module cache. Shell commands are not fenced this way.

## Budget, quota and one node per repo

`ops/budget.py` keeps unattended runs from spending more than agreed or fighting over a repo.

**Daily budget.** The agents' reported cost of every run today, attempts and reviews, adds up from the audit ledger. It stays under `daily_budget_usd` ($20), and the day starts at 00:00 `budget_tz` (Asia/Jakarta).
- Each attempt and each review may spend only what is left of the day, and never more than `budget_usd` ($6). So the cap holds inside a run too.
- When less than $1 is left, no node starts. A node that is between attempts goes back to `status:todo` with `STATUS: paused`, and is not counted as failed.
- Claude now prints its result as JSON (`--output-format json`), so its cost is recorded like OpenCode's. A Claude run stopped before it finished (time, `ops/stop`) is counted at its whole allowance, the most it could have spent, and the record says `cost_estimated`.
- Reported cost is what the agent says the run would cost at API prices. On a subscription plan it is a measure of use, not a bill.

**Quota pause.** When an agent's plan or API says the quota is gone ("you've hit your limit", 429, rate limit), the run is handed back and that agent is paused.
- The pause lasts until the reset the message names, plus 2 minutes, or `quota_pause_min` (60) when it names none.
- The attempt is given back, so a quota never spends `max_attempts`.
- The pause holds only that agent. A Claude limit holds client nodes, and OpenCode still runs server nodes.
- A review that hits the quota pauses the reviewer. The PR still reaches you, marked "review unavailable".
- `ops/stop --clear` ends every pause early. A balance that ran out (402) is not a quota: it escalates to you as before.

**One node per repo.** `ops/run` holds a lock per repo while a node runs. A second runner for the same repo stops before starting anything, and the ledger records `dispatch.busy`. The kernel drops the lock if a runner dies, so it never goes stale.

## Tiers: gate by cost of undo

`ops/gate` computes the tier from changed paths in `ops/tiers.yaml`. Agents can't declare it.

| Tier | Example | To merge |
|---|---|---|
| 1 | Docs, tests | Green; auto-merge when enabled |
| 2 | Pages, components, handlers | Green + independent review with no High; auto-merge when enabled |
| 3 | Contract, auth, migrations, CI, deploy, email, agent rules | Green + review + you |

`automerge` in `ops/tiers.yaml` starts empty. Add 1, then 2, after the pilot targets below pass. Removing a number turns it off again.

The control plane has its own CI (`.github/workflows/ops.yml`, check `ops`). It runs `ops/tests`, including the policy tests in `ops/tests/test_policy.py`. These test the real `ops/tiers.yaml` and `ops/egress-allowlist`:
- sensitive paths stay tier 3;
- product code is never tier 1;
- `automerge` never holds 3;
- every allowlist entry is one exact hostname, never an IP;
- sandbox and run limits stay in their ranges.

Loosening a rule past its bound means changing the test in the same PR, where the change is easy to see.

## Findings

Every finding takes exactly one path:

| Finding | Action | Effect |
|---|---|---|
| Inside the fence, blocking | Fix in this node | None |
| Outside the fence, blocking | Create a `type:gap` issue, set `STATUS: gap` | Runner adds it to `blocked_by`; the gap runs first |
| Not blocking, has an owner | Comment the exact fix on the owning issue | Owner starts informed |
| Not blocking, no owner | Create a `type:gap` issue | Scheduled by the graph |
| Needs a decision | `STATUS: needs_human` | Appears in `ops/triage`; other nodes continue |

There is no `PROGRESS.md` or `FINDINGS.md`. Status is computed, handoffs live on issues, and reusable knowledge goes in `docs/LESSONS.md`.

## Frozen work

A closed node stays closed. A later change is a new node.

## Contract

The server repo is the source. `contract.lock` in the client pins a server commit and file hashes, and CI compares against that commit, so a server merge can't turn unrelated client PRs red. `ops/run` opens the sync PR when the server moves.

## Learning

`ops/triage --apply` opens a `skill-update` node for lessons marked as repeats with no follow-up PR. Prefer a check or lint rule over more text.

## Planning a feature or revamp

Interactive skills live in this repo, not in the app repos: `grilling` (a round-by-round interview until every design decision is settled), `domain-modeling` (`GLOSSARY.md` and ADRs) and `grill-with-docs` (both). They ask a human questions, so a headless agent must never load them. The settled plan becomes nodes: issues with `fence`, `blocked_by` and `checks`.

## Mockups before a UI node

Before a UI node runs, its screens are mocked up here with the human and published as an artifact. The approved mockup link goes into the node's issue as the visual reference, and `talent-scout-client/DESIGN.md` still wins on any conflict. The skills for this live in this repo:

- `impeccable`: a symlink to the client's pinned copy (`talent-scout-client/.claude/skills/impeccable`), so mockups follow the same rules as the agent that builds the page. Only `critique` and `audit` are used here.
- `prototype`: several genuinely different versions of one UI piece behind a picker. Variants use the `DESIGN.md` tokens and never touch app code.
- `break-ui`: the same screen with realistic worst-case data, such as long names, empty tables or every criterion `needs_manual`.

Like the planning skills, they need a human, so a headless agent must never load them.

## Audit ledger

`ops/run` appends one record per step to the audit ledger (`ops/ledger.py`):
- `run.started`, `attempt` (with cost and refused egress, or why it was stopped), and `gate` (tier, pass, failed checks);
- `pr.opened`, `ci`, `review.run` (the reviewer's cost), and `review` (finding and High counts);
- `queue.paused` and `queue.resumed` (which agent, why, until when, and who ended it), and `dispatch.busy`;
- `ready`, `escalated`, `handed_back`, `parked`, and `merged`, with who merged and which checks were red at the time.

**Where it lives:** `~/.local/state/talent-scout-ops/audit-YYYY-MM.jsonl` (or `$OPS_LEDGER_DIR`). That's outside the repo and outside every sandbox mount, and the files are never deleted.

**Why tampering shows:**
- Each record carries the hash of the one before it, so a changed, dropped or reordered line breaks the chain.
- The current head (seq and hash) goes as a hidden line into every PR body and handoff comment. GitHub's timestamps witness it, and `ops/report --verify` checks that the ledger still holds every published head. That also catches a cut-off tail.

**What a person does goes in too, through `ops/report --note`:**
- an **intervention**: you or the lead agent fixed or decided something a node could not;
- an **escaped-high**: the verifier found a High finding that the review missed.

`ops/report` turns all this into one row per node, plus the tier 1 auto-merge targets below.

## Pilot targets before enabling tier 2 auto-merge

- Under 1 human intervention per tier 1–2 issue on average
- No merge with failing CI, and no High finding that escaped
- Most gaps found at SCOPE, not mid-implementation

Tier 1 auto-merge needs the last 10 merged tier 1 nodes in the ledger to meet the first two lines. `ops/report` shows where they stand.

## Setup that only a human can do

- `ops/env/client.env.agent` and `ops/env/server.env.agent`: agent credentials only, pointing at a separate Supabase project, with `LLM_MODE=fake` and `EMAIL_MODE=log`. They are gitignored.
- Required status checks on `main`: client `checks` and `contract`, server `test`, and in this control plane `ops`.

## Sandbox

Every agent run (worker and reviewer), and every command the orchestrator runs on code from a worktree (green checks, the red check, dependency installs), runs in rootless Podman. Container is the only mode, and it fails closed: if anything below is missing, nothing runs on the host. The reasons are in `docs/adr/0002` to `0005`, and the vocabulary is in `GLOSSARY.md`.

What an agent **can** do:
- read and change its own clone (`ops/.state/clones/<repo>-<N>`), commit there, and run go, pnpm, git, gh and rg;
- reach the hosts in `ops/egress-allowlist`, through the `ts-egress` proxy only;
- read and write issues with its own GitHub token.

What it **cannot** do:
- see your home directory, `ops/env`, your SSH key, your gh or git config, or either of your repositories;
- reach any other host, resolve outside names, or push;
- use more than its limits (`sandbox_cpus`, `sandbox_memory` and `sandbox_pids` in `ops/tiers.yaml`, plus `timeout_min` and `budget_usd`).

Its commits come back as a git bundle, fetched into your worktree with hooks off.

Day to day:
- `ops/sandbox build` rebuilds the client, server and egress images and warms the Go build cache. Run it after you change `ops/images/Containerfile`. The orchestrator refuses an image whose tag does not match the file.
- `ops/sandbox warm` refills the Go build cache from `main`, for example after a large dependency change.
- `ops/run --check-sandbox` proves the walls from inside each image. `ops/run` also runs it before the first node of every session.
- A refused host shows up in the node's `ops/.state/egress/<node>.log`, in the handoff's `EGRESS` field and in `ops/triage`. To allow it, add the exact host to `ops/egress-allowlist` in a reviewed PR. The proxy picks up the change on its next start.
- The Claude agent needs `ops/env/claude.oauth-token` (from `claude setup-token`, mode 600). OpenCode uses a throwaway copy of its login, with a spend cap on the subscription.

## Agent GitHub token

This is the legacy setup (`github_identity: legacy`). The GitHub Apps section below replaces it.


Agents never use your `gh` login. `ops/run` gives them their own token and an allowlisted env (`AGENT_ENV_KEEP` in `ops/run`); everything else in your shell, such as database URLs and API keys, is dropped.

- **Scope:** a fine-grained token for `talent-scout-client` and `talent-scout-server` only, with Issues read/write and Metadata read. No Contents, Pull requests or Workflows access. Agents can read and file issues; GitHub itself refuses their pushes. Pushing, opening PRs and merging stay with `ops/run`, which uses your `gh` (or the `ts-orchestrator` App once `github_identity: apps`, see below).
- **Where:** `ops/env/agent.gh-token`, gitignored, mode 600. `ops/run` refuses to start an agent without it, or if it is not a `github_pat_` token.
- **Save it** without it showing on screen or in shell history:

  ```bash
  read -rs T && printf '%s' "$T" > ops/env/agent.gh-token && chmod 600 ops/env/agent.gh-token && unset T
  ```
- **Check it:** `ops/run --check-agent-auth` must print "read issues ok" and "push refused by GitHub (403)" for both repos.
- **Rotate** before it expires (90 days): generate a new token with the same scope, save it the same way, run the check, then delete the old token on GitHub.

## GitHub Apps and encrypted secrets

`ops/tiers.yaml` sets two switches (ADR 0006):
- **`github_identity`:** `legacy` uses the agent token above and your `gh` login. `apps` uses short-lived tokens of two GitHub Apps.
- **`secret_store`:** `files` reads plain files under `ops/env`. `creds` reads files encrypted with `systemd-creds --user` under `~/.config/talent-scout-ops/secrets`.

### One-time setup (human)

1. **Create the App `ts-agent`.** Go to GitHub, then Settings, Developer settings, GitHub Apps, New GitHub App.
   - **Homepage URL:** the control-plane repo URL.
   - **Webhook:** turn off "Active".
   - **Repository permissions:** Issues **Read and write**. Metadata becomes read-only by itself. Nothing else.
   - **Where can this App be installed:** Only on this account.
   - Create the App, and note its **App ID**.
   - **Generate a private key.** A `.pem` file downloads.
2. **Create the App `ts-orchestrator`** the same way, with these repository permissions:
   - Contents **Read and write**;
   - Pull requests **Read and write**;
   - Issues **Read and write**;
   - Checks **Read-only**;
   - Commit statuses **Read-only**.

   Leave Workflows and Administration at **No access**. Note its App ID, and generate its private key.
3. **Install both Apps** (App page, then Install App) on **only** `talent-scout-client` and `talent-scout-server`.
4. **Encrypt the secrets.** `--remove` deletes each plain file once it decrypts back the same:

   ```bash
   ops/creds import agent-app-key ~/Downloads/ts-agent.*.private-key.pem --remove
   ops/creds import orchestrator-app-key ~/Downloads/ts-orchestrator.*.private-key.pem --remove
   ops/creds import claude-oauth-token ops/env/claude.oauth-token --remove
   ops/creds import client-env-agent ops/env/client.env.agent --remove
   ops/creds check
   ```
5. **Switch.** In `ops/tiers.yaml`, set `github_identity: apps`, the two App IDs, and `secret_store: creds`. The policy tests then require both IDs, and require `creds` while on `apps`.
6. **Check.** `ops/run --check-agent-auth` must still print "read issues ok" and "push refused by GitHub (403)" for both repos. `ops/run --check-sandbox` must show the secret and ledger directories as hidden.
7. **Clean up** once a node has run on the Apps:
   - delete the old fine-grained token on GitHub, and `ops/env/agent.gh-token`;
   - in each repo's `main` ruleset, require 1 approval, and give the bypass for you to pull requests only.

### What changes in daily use

- **PRs come from `ts-orchestrator[bot]`.** You approve them, then merge.
- **The orchestrator can't edit CI or branch rules.** A node that needs that is escalated to you.
- **Not covered here:** OpenCode's own login (`~/.local/share/opencode/auth.json`) stays where OpenCode keeps it. The sandbox still gets only a throwaway copy of it per run.
