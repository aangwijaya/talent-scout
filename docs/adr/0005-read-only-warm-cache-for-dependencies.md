---
status: proposed
---

# Dependencies come from a read-only warm cache

Go modules and the pnpm store are filled by the orchestrator on the host and mounted read-only into the sandbox. Anything a run downloads beyond that goes to a throwaway layer that is discarded with the run. A shared read-write cache would be faster, but one run could poison it for every later run, and that poisoning would outlive the sandbox. A run with no cache at all would re-download everything for every node.

## Consequences

- A node that adds a dependency downloads it through the egress allowlist on every run until the orchestrator refreshes the warm cache from the merged lockfile.
