---
status: proposed
---

# Podman rootless as the sandbox runtime

The host is WSL2 with no working Docker (Docker Desktop's WSL integration is off), no Podman and no gVisor; bubblewrap and unprivileged user namespaces are available. We chose rootless Podman: real container isolation, no root daemon, the container user maps to the human's user, and images are reproducible from a Containerfile per repo in `ops/sandbox/`.

## Considered Options

- **Docker Desktop with WSL integration:** easiest, but it depends on a Windows app being up and its daemon runs as root.
- **bubblewrap:** already installed and light, but network isolation is all-or-nothing, so an allowlist proxy would need hand-built network namespaces and bridges, which are fragile.

## Consequences

- The images carry their own toolchain (Go from a tarball, because the host's snap Go does not run in a container), Node, pnpm, Playwright browsers and both agent CLIs.
- There is one image per repo, built from `ops/sandbox/Containerfile.<repo>`, with every version pinned and tagged by the Containerfile's hash. It is rebuilt only by an explicit `ops/sandbox build`, so agent CLI upgrades are deliberate. The orchestrator refuses to run if the exact tag is missing.
- Installing Podman is a one-time human step on the host.
