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
