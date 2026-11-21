# WORKFLOW: Talent Scout agent graph

The only source for how work runs. App repos: `aangwijaya/talent-scout-client` (Claude) and `aangwijaya/talent-scout-server` (OpenCode). Design basis: harness says what an agent can do, the loop says what is true, the graph says what happens next.

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
ops/stop                     # hand work back and halt; ops/stop --clear to re-enable
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

Models are pinned in `ops/tiers.yaml`: `claude_model` and `claude_effort` for Claude, `opencode_model` and `opencode_effort` (sent as `--variant`) for OpenCode (`opencode models --verbose` lists the ids). They apply to both the worker and the reviewer role. Each agent call is limited by `timeout_min` and, for OpenCode, by `budget_usd` summed from its cost events. Claude enforces `budget_usd` itself. Outside the worktree, agents may only write `ops/.state/handoff/` with their edit tools and read the Go module cache. Shell commands are not fenced this way.

## Tiers: gate by cost of undo

`ops/gate` computes the tier from changed paths in `ops/tiers.yaml`. Agents can't declare it.

| Tier | Example | To merge |
|---|---|---|
| 1 | Docs, tests | Green; auto-merge when enabled |
| 2 | Pages, components, handlers | Green + independent review with no High; auto-merge when enabled |
| 3 | Contract, auth, migrations, CI, deploy, email, agent rules | Green + review + you |

`automerge` in `ops/tiers.yaml` starts empty. Add 1, then 2, after the pilot targets below pass. Removing a number turns it off again.

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

## Pilot targets before enabling tier 2 auto-merge

- Under 1 human intervention per tier 1–2 issue on average
- No merge with failing CI, and no High finding that escaped
- Most gaps found at SCOPE, not mid-implementation

## Setup that only a human can do

- `ops/env/client.env.agent` and `ops/env/server.env.agent`: agent credentials only, pointing at a separate Supabase project, with `LLM_MODE=fake` and `EMAIL_MODE=log`. They are gitignored.
- Required status checks on `main`: client `checks` and `contract`, server `test`.

## Sandbox (being built)

Agents, reviewers and every command that runs code from a worktree are moving into a fail-closed sandbox: rootless Podman, an egress allowlist proxy, a per-node agent clone and a read-only warm cache. Until the pilot ends, `sandbox: host | container` in `ops/tiers.yaml` picks the mode. Host mode is removed after the pilot, and container mode never falls back to the host. The reasons are in `docs/adr/0002` to `0005`, and the vocabulary is in `GLOSSARY.md`.

Stages, each its own PR to this repo:
1. Strip unused secrets from `ops/env/*.env.agent`, and run green and red checks with the filtered agent environment.
2. Podman images per repo (`ops/sandbox build`), runtime and limits, and `ops/run --check-sandbox`.
3. Egress proxy with the allowlist and per-node logs. Refusals are surfaced in the handoff and `ops/triage`.
4. Agent clone, with commits fetched into the human's repository with hooks disabled.
5. Pilot on two nodes per repo, then remove host mode.

Once stage 5 lands, this section will say what an agent can and cannot do, how to build images, and how to add an allowlisted host.

## Agent GitHub token

Agents never use your `gh` login. `ops/run` gives them their own token and an allowlisted env (`AGENT_ENV_KEEP` in `ops/run`); everything else in your shell, such as database URLs and API keys, is dropped.

- **Scope:** a fine-grained token for `talent-scout-client` and `talent-scout-server` only, with Issues read/write and Metadata read. No Contents, Pull requests or Workflows access. Agents can read and file issues; GitHub itself refuses their pushes. Pushing, opening PRs and merging stay with `ops/run`, which uses your `gh`.
- **Where:** `ops/env/agent.gh-token`, gitignored, mode 600. `ops/run` refuses to start an agent without it, or if it is not a `github_pat_` token.
- **Save it** without it showing on screen or in shell history:

  ```bash
  read -rs T && printf '%s' "$T" > ops/env/agent.gh-token && chmod 600 ops/env/agent.gh-token && unset T
  ```
- **Check it:** `ops/run --check-agent-auth` must print "read issues ok" and "push refused by GitHub (403)" for both repos.
- **Rotate** before it expires (90 days): generate a new token with the same scope, save it the same way, run the check, then delete the old token on GitHub.
