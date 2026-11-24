---
status: accepted
---

# Nodes merge by themselves into `dev`, and production changes only through a release the human merges

Until now the human merged every node PR into `main`, and `main` is what Railway and Vercel deploy. That kept production safe, but the human became the queue: every node waited for a person.

Now the app repos get an integration branch, `dev`, as their default branch. Nodes start from it and merge into it. A tier 1 or 2 node merges by itself when three things agree:
- CI is green;
- the independent review found no High;
- the verifier, now an orchestrator step, says `merge`.

The verifier runs headless in the sandbox, on its own fresh copy of the branch. There it checks the issue's criteria, weighs the review's findings and sabotages the tests. Tier 3 (contracts, auth, migrations, CI, the agents' rules) still needs the human, even into `dev`, because a wrong contract spreads to the other repo and to every later node. `main` changes only through a release PR from `dev` that the human merges: server first, then client.

## Considered Options

- **Auto-merge tier 1 straight into `main`, after 10 clean nodes (the earlier plan):** no new branch, but every auto-merge is a production deploy, and the human still merges every tier 2 node.
- **Keep merging everything by hand:** the safest, but the human stays the bottleneck, and verifying every PR by hand costs more attention than reading one release summary.
- **A staging deployment of `dev`:** catches what CI can't, but it is a second Railway service, and a Vercel preview of `dev` would point at the production API with a contract that may differ. It is the next step if releases start breaking production.

## Consequences

- **Issues still close by themselves.** GitHub closes an issue only when a PR merges into the default branch, and `blocked_by` waits for closed issues. That is why `dev` becomes the default branch.
- **One escaped High turns tier 2 auto-merge off.** An escaped High is one the review missed and the verifier or the human found. Auto-merge stays off until the human turns it back on (`ops/report --automerge on`), and both steps are in the ledger.
- **A bad node comes out with `ops/revert`.** It opens a revert PR into `dev` for the human, reopens the issue, and asks every node merged after it for a second look. `main` is untouched until the next release.
- **The human reads a release summary, not the combined diff.** `ops/release` lists every node since the last release with its tier, review, verdict, cost and who merged it, from the audit ledger. It refuses while CI on `dev` is red, or when the client's contract pins server code that is not in the server's `main` yet. The lead agent writes the summary on top.
- **The switch waits for the human's setup.** `integration_branch: main` keeps everything as it was, and nothing merges by itself into `main`. It changes to `dev` once the branches, default branch and rulesets are in place.
- **The control plane keeps `main` only.** Its changes are the orchestrator's own rules, so every one stays a human merge.
