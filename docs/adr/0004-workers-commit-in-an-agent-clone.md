---
status: accepted
---

# Workers commit in an agent clone, never in the human's repository

A git worktree points at the main repository's `.git`, so a worker that can commit there can also write hooks or config that the host later runs during `git push` with the human's SSH key. Inside the sandbox the worker therefore commits in a per-node agent clone with its own `.git`. Afterwards the orchestrator fetches the commits into the human's repository with hooks disabled (`core.hooksPath` pointing at an empty directory) and never reads that clone's config.

## Considered Options

- **Mount the main `.git` read-write:** simplest, but it leaves the hook and config path open.
- **Workers never commit, and the orchestrator commits the working tree:** this loses the worker's own commit structure and messages, for no extra safety over a clone.
