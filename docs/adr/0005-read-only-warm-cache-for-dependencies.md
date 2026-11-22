---
status: proposed
---

# Dependencies come from a read-only warm cache

Go modules and the pnpm store are filled by the orchestrator on the host and mounted read-only into the sandbox. Anything a run downloads beyond that goes to a throwaway layer that is discarded with the run. A shared read-write cache would be faster, but one run could poison it for every later run, and that poisoning would outlive the sandbox. A run with no cache at all would re-download everything for every node.

## Consequences

- A node that adds a dependency downloads it through the egress allowlist on every run until the orchestrator refreshes the warm cache from the merged lockfile.
- The Go build cache is the sandbox's own (`ops/.cache/sandbox/go-build`), not the host's. The host cache is keyed to the host's Go install, so the image's Go misses all of it. `ops/sandbox warm`, which `ops/sandbox build` also runs, fills it by compiling `main` in the sandbox. It brings `go build` from about 40 s down to about 5 s.
