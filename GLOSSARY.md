# Agent workflow

How one human and a team of AI agents build the Talent Scout client and server from this control plane. Product terms (applicant, session, rubric) belong to the app repos' docs, not here.

## Roles

**Human**:
The person who decides what to build, holds every secret and production access, runs live gates, and merges every PR.
_Avoid_: user, operator, owner

**Lead agent**:
The Claude Code session that works with the human in the control plane: it plans, writes nodes, and verifies PRs, but never dispatches work.
_Avoid_: supervisor, orchestrator, manager

**Orchestrator**:
The deterministic program (`ops/run`) that picks ready nodes, prepares the sandbox, runs gates, pushes, and opens PRs. It is code, not a model.
_Avoid_: runner, scheduler, supervisor

**Worker**:
The coding agent that implements one node: Claude Code for the client, OpenCode for the server.
_Avoid_: builder, client agent, server agent (when the role, not the repo, is meant)

**Reviewer**:
A read-only session of the other worker's agent that reviews a PR from the issue and the diff alone.
_Avoid_: critic, cross-reviewer

**Verifier**:
The lead agent's sub-agent that checks a ready PR against its issue, the review and CI, and sabotages its tests.
_Avoid_: QA agent, tester

## Work

**Node**:
One GitHub issue carrying a fence, its real dependencies and its checks; the unit the orchestrator runs.
_Avoid_: task, ticket, job

**Node graph**:
The dependency graph of open nodes, computed from GitHub (`blocked_by`); it decides which node is ready next.
_Avoid_: loop-graph, task graph, backlog

**Node loop**:
The cycle inside one node: write the checks and see them fail, implement, gate, and retry or escalate.
_Avoid_: loop-graph, iteration

**AI graph**:
The product's own graph of model and code nodes that interviews and scores an applicant; it lives in the server, not in the control plane.
_Avoid_: loop-graph, agent graph (when the product is meant)

**Fence**:
The set of paths a node may change.
_Avoid_: scope, allowlist

**Gap**:
A node created because a blocker lies outside another node's fence; it runs before the nodes it unblocks.
_Avoid_: follow-up, side task

**Handoff**:
The structured report a worker writes at the end of a run: what changed, what was tested, what it could not do.
_Avoid_: summary, PR description

**Gate**:
The orchestrator's code checks on a finished run: fence, contract, secrets, red proof and green commands.
_Avoid_: CI (CI is GitHub's run on the PR), validation

**Tier**:
How costly a change is to undo, computed from its paths; it decides who must approve the merge.
_Avoid_: risk level, priority

**Dispatch**:
The human's signal that one node may run now; the orchestrator starts a node only on a dispatch from the human. A dispatch from anyone else is refused.
_Avoid_: trigger, kick off, schedule

**Interrupted**:
A node whose run stopped because the orchestrator itself stopped (a crash, a restart, the machine asleep), not because of its own work. It runs again once, then goes to the human.
_Avoid_: failed (a failure is the node's own), stopped (the human's choice)

**Re-armed**:
An escalated node the human sent back with the `run` label: it runs again from its first attempt, keeping the lessons of the last run.
_Avoid_: retried, reset

**Integration branch**:
The branch nodes merge into, by themselves when their tier allows; production never deploys from it.
_Avoid_: dev (the branch's name, not the role), staging

**Release**:
The human-merged change that moves the integration branch into the branch production deploys from.
_Avoid_: deploy, promotion, sync

**Live gate**:
A run of the real product graph against real models, started by the human, to check prompt or model changes.
_Avoid_: smoke test, eval

## Isolation

**Sandbox**:
The isolated environment in which a worker, a reviewer, and every command that executes code from a worktree run, with no access to the human's secrets, files or network beyond the egress allowlist.
_Avoid_: container (the implementation), jail, VM

**Egress allowlist**:
The only network destinations reachable from a sandbox; everything else is refused.
_Avoid_: firewall rules, whitelist

**Agent clone**:
The per-node repository copy a worker commits in inside the sandbox; the orchestrator fetches commits from it and never runs anything it contains.
_Avoid_: worktree (the human's checkout), fork

**Warm cache**:
The read-only dependency cache the orchestrator fills on the host and the sandbox reads; anything a run downloads beyond it is discarded with the run.
_Avoid_: shared cache, module cache

## Oversight

**Audit ledger**:
The append-only, tamper-evident record of what the orchestrator did and decided: dispatches, runs, gates, costs, refusals and merges.
_Avoid_: log (a run's own output), history

**Daily budget**:
The most the agents' reported cost may add up to in one day, counted from midnight Jakarta time. A run may spend only what is left of it.
_Avoid_: cost limit (one run has its own, the run budget)

**Pause**:
A hold on starting new work for one agent until its plan's quota comes back. The interrupted node goes back to the queue and is not counted as failed.
_Avoid_: stop (the human's kill switch), parked (a node waiting behind a gap)

**Policy test**:
A test of the control plane's own rules (tiers, the egress allowlist, limits) that fails when a change loosens one past its bound.
_Avoid_: lint, config check
