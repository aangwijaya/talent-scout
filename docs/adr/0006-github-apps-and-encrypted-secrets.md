---
status: accepted
---

# GitHub Apps act for the agents and the orchestrator, and secrets are encrypted at rest

Until now agents used a 90-day fine-grained token kept in a plain file, and the orchestrator pushed and opened PRs with the human's own `gh` login and SSH key. So every PR looked like the human wrote it, and GitHub could not require the human's approval on it: nobody can approve their own PR.

We replace both with GitHub Apps:
- **ts-agent:** Issues only.
- **ts-orchestrator:** Contents, Pull requests and Issues; Checks and Commit statuses read-only. It has no Workflows or Administration permission.

Every token is an installation token minted per run for only the repos and permissions that run needs, and it expires within an hour. The Apps' private keys, the Claude token and the agent env file are encrypted with `systemd-creds --user`, so no long-lived secret sits in a plain file.

## Considered Options

- **Keep the PAT and the human's login:** nothing to set up, but PRs can't be required to have the human's approval, and a leaked token works for months.
- **A machine user account:** a second GitHub account with its own PAT. It's a real identity to manage, still long-lived, and it uses a seat.
- **Vault or a cloud secret manager:** stronger, but it's a service to run or pay for. On one machine it guards the same key that `systemd-creds` does.

## Consequences

- **Approval is enforced by GitHub, not by habit.** PRs come from `ts-orchestrator[bot]`, so a branch rule can require the human's approval. The human's own PRs merge through an audited bypass.
- **The orchestrator can't change CI or branch rules.** A node that needs a workflow change goes to the human.
- **Limit of encryption at rest:** anyone already running as this user on this machine can decrypt too. It stops a secret leaking through a backup, a `cat` or a wrong `git add`. The one-hour tokens limit what a leak is worth.
- **Migration:** `github_identity: legacy` and `secret_store: files` keep the old way until the Apps exist and the secrets are imported.
