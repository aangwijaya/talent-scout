---
status: accepted
---

# The orchestrator runs as a service on this machine, and the human dispatches nodes with a label

Until now a node ran only while the human had a terminal open: `ops/run` by hand, or `ops/auto` in tmux. Now the orchestrator is a systemd user service on the same WSL machine (`ops/service`). It looks at GitHub every 90 seconds, tidies up after merges, opens the contract sync PR, and starts a node when the human puts the `run` label on its issue, from a laptop or from GitHub's phone app.

A label is only text: anyone with Issues write can add one, the agents included. So the service asks the issue timeline who added it, and starts the node only if that was the human's account (`human_login`). Any other actor's label comes off, the issue gets a comment, and the ledger records `dispatch.refused`. A contract-change node also needs its `## Decision` section to start with "Approved", in an issue body last written by the human. The agents can edit issue bodies, so GitHub's edit history decides, not the text.

## Considered Options

- **A VPS (about €8 a month):** always on even when the PC is off, but it costs money, and this machine is already stronger than a cheap VPS. Moving later means moving one service.
- **GitHub Actions:** no machine to keep awake, but the Claude token and rootless Podman would have to live on a runner, and an agent session runs up to 60 minutes.
- **Webhooks instead of polling:** faster, but the PC has no public endpoint. Polling every 90 seconds uses about 3,000 of the 5,000 API calls per hour that GitHub allows, at most.

## Consequences

- **Nothing runs while the PC is off or asleep.** A Windows scheduled task keeps WSL alive at login, and the PC is set not to sleep while plugged in. After a pause the service carries on from GitHub's state, because GitHub is where that state lives.
- **One node per repo, so two at most.** Each node runs in a child `ops/run --node X --once --auto`, the same path `ops/auto` takes. A human runner holding a repo's lock keeps the service off that repo.
- **An interrupted node runs again once.** A node left `status:in-progress` or `status:review` with no runner holding its repo is "interrupted" (a crash, a restart, the PC asleep). It goes back to the queue once, and that attempt still counts toward `max_attempts`. The second time it goes to the human.
- **The kill switch has two steps.** `ops/stop` holds dispatch, and a running node finishes. `ops/stop --now` also stops running nodes and removes their containers. `ops/stop --resume` reopens.
- **The human hears about it on GitHub.** The service comments when a node starts, is refused, is interrupted, is ready or needs a decision. The `run` label comes off once the node is ready, escalated or stopped by the human, so adding it again means "run it again".
- **A contract-change whose body the orchestrator edited needs the human to save it once more.** Adding a gap to `blocked_by` makes the orchestrator the last writer. That is the safe way to fail.
