---
status: accepted
---

# The orchestrator is code, not a model

Many multi-agent setups let an LLM decide which agent works next. Here the orchestrator (`ops/run`) is deterministic code: it computes ready nodes from GitHub, runs the gates, and is the only thing that pushes or opens PRs. The lead agent plans with the human and verifies PRs but never dispatches work. We chose this so that no model can decide to skip a gate, widen a fence or merge, and so that every step is reproducible from GitHub state.

## Consequences

- Adding a new kind of step means changing `ops/run`, not prompting an agent.
- The lead agent and the orchestrator are separate roles in docs and slides; "supervisor" and "runner" are retired terms (see `GLOSSARY.md`).
