# Talent Scout — Master Plan (1 Week / 7 Days)

> The parent document for both repos. The root folder `talent-scout/` is **not** a git repo, so copies of the contract documents (`docs/API.md`, `docs/EVENTS.md`, `docs/PRD.md`) are kept **identical** in both repos. Source of truth: `talent-scout-server/docs/`. If the contract changes, change it on the server first and then copy it to the client in a commit the same day.

## 1. Goals

A portfolio project that shows 4 things concretely and can be demoed in 1 minute:

| Showcase | How it shows up in Talent Scout |
|---|---|
| **Loop** | Produce → check → correct. Checks are evaluated by code (valid JSON schema, evidence quotes really exist in the transcript, time/question-count limits), not by a model saying "looks good". |
| **Graph engineering** | Nodes typed as splitter / worker / code node / gate. Edges are only real data dependencies. Execution is recorded in `node_events`. |
| **Multi-agent** | Planner, Interviewer, Judge, Scorer — each with its own prompt & responsibility. |
| **Multi-model** | The model per node is set in config (`config/models.yaml`), via OpenRouter. |

Product: **recruiter-side AI screening**. The applicant applies at `/apply/[jobId]` → gets a single-use session link (MVP: the recruiter copies it from the dashboard, `EMAIL_MODE=log`; Resend email optional) `https://talentscout.vercel.app/s/<token>` (valid 7 days) → a short screening chat with the AI (~10–15 minutes) → rubric-based scores with evidence quotes → the recruiter decides shortlist/reject in the dashboard. The UI shows the graph running live via SSE.

## 2. Repos & Roles

| Repo | Contents | Built by |
|---|---|---|
| `talent-scout-server` | Go API on Railway, agent graph, Supabase Postgres + Storage, SSE (Resend email optional) | **OpenCode + DeepSeek** (via OpenRouter, separate capped key) |
| `talent-scout-client` | SvelteKit (server-side BFF, `adapter-vercel`) + TypeScript on Vercel; one app: `/dashboard/...` (recruiter), `/apply/[jobId]`, `/s/[token]` (AI session) | **Claude** |
| — | Plan, docs, review, product decisions | **Grok Bot** (planning only, writes no code) |
| — | Account/key setup, running agents, PR review, merge, recording the demo | **You** |

Per-agent briefs: `talent-scout-server/AGENTS.md` and `talent-scout-client/CLAUDE.md`. Daily checklist: `PLAN.md` in each repo. How the agents work (issue → branch → PR → review, labels, learning loop): **`WORKFLOW.md`**. Unit of work = a GitHub issue; the issue list is in `WORKFLOW.md` §8.

## 3. 7-Day Timeline

### 1-week scope (cut down from the 2-week plan)

- **1 seeded job** (`go run ./cmd/seed` from `config/seed-job.yaml`). No job create/edit UI; `POST /jobs` & `PATCH /jobs/{jobId}` = post-MVP. `GET /jobs`, `GET /jobs/{jobId}`, `GET /jobs/{jobId}/public` remain.
- **Email optional.** Default `EMAIL_MODE=log`: the session link is printed to the log and the recruiter fetches it via the **Get invite link** button in the dashboard (`POST /applications/{id}/resend-invite` → `invite_url`).
- **Simple recruiter auth:** 1 password from the `RECRUITER_PASSWORD` env → a stateless 12-hour HMAC token (no refresh; logout = clear the cookie). The SvelteKit server keeps the token in an httpOnly cookie; the recruiter run SSE uses a 60 s `?sse_token=` from `POST /auth/sse-token` (see `talent-scout-client/docs/SERVER-SIDE.md`).
- **Simplified loops:** no `:free` model fallback, no interviewer regenerate, no judge retry (invalid JSON → default `sufficient`), planner retry max 1, LLM HTTP errors get 1× retry on the same model. **Kept:** Scorer + Evidence Check max 3 retries, Judge max 2 follow-ups per topic.
- **Playwright = 1 smoke test** (mock): apply → chat → review.

| Day | Server (OpenCode) | Client (Claude) | Done criteria |
|---|---|---|---|
| **1** | Contract: `openapi.yaml` from `docs/API.md`; Go skeleton + `/api/v1/health`; Supabase migration with 8 tables (**RLS on all tables, no policies**); job seed; minimal CI | SvelteKit server-side skeleton on `adapter-vercel` (routes `/apply`, `/s`, `/dashboard`), TS types from `openapi.yaml`, design tokens from `DESIGN.md`, mock layer, minimal CI, Vercel preview | `go vet`/`go test` green; `pnpm check` green; Supabase Security Advisor 0 RLS findings; Vercel preview routes render server-side; contract identical in both repos |
| **2** | Apply + CV upload (private Storage) + session token + `EMAIL_MODE=log`; recruiter auth; graph runner + Planner/plan_check + Interviewer + SSE | All pages on mocks: apply, session `/s/[token]` (mock chat stream), login, dashboard list | Handler tests + PDF ≤ 2 MB validation; expired/used token tests; runner with `LLM_MODE=fake` emits events in `seq` order |
| **3** | Judge loop + Guard; Scorer + Evidence Check (max 3); recruiter endpoints (applications, detail, decision, resend-invite, graph-runs snapshot/SSE); contract conformance tests | Applicant detail + review (scores, evidence, decision, Get invite link), live graph view (`@xyflow/svelte`) from the `EVENTS.md` §6 replay; 1 Playwright smoke | Tests: fake quote → `loop.retry` → success/`needs_manual`; Guard without LLM; Playwright smoke green on mocks |
| **4** | Integration + prompt/loop tuning with real models | Switch to the local API (`USE_MOCK=false`), fix mismatches | The apply → link → session → score → shortlist flow works end-to-end locally; ≥ 1 `loop.retry` visible in the graph |
| **5** | Railway deploy (env `DATABASE_URL` = session pooler) + server README | Vercel deploy (private env `API_BASE_URL`, `USE_MOCK=false`, `BFF_SHARED_SECRET`) + client README | Public health OK; 1 real session succeeds in production; someone else can run it locally from the README in < 10 minutes |
| **6** | Bug fixes | Bug fixes + polish | **You:** record a ≤ 60-second demo (apply → link → chat → live graph → scores + evidence → shortlist), linked in the README |
| **7** | Buffer | Buffer | No new features; only fixes & documentation |

Days 2–3: server and client are built **in parallel**; the client doesn't wait for the server because the mocks follow the contract. If running late, cut first (in order): detailed graph animation → Get invite link (use the link from the log) → UI polish.

## 4. Green Checks (mandatory before merge)

- **Server:** `go vet ./...`, `go test ./...`, contract conformance tests (handler responses validated against `openapi.yaml`).
- **Client:** `pnpm check` (svelte-check), `pnpm lint`, `pnpm test:e2e` (1 Playwright smoke against mocks).
- Contract in sync: `docs/API.md`, `docs/EVENTS.md`, `docs/PRD.md` identical in both repos.
- CI details (GitHub Actions) and other gates: `WORKFLOW.md` §6.

## 5. Your Setup Checklist (once at the start)

In WSL Ubuntu-26.04 (`/home/agni/projects/talent-scout`):

- [ ] `git config --global user.name` / `user.email` are correct
- [ ] GitHub CLI: `gh auth login` (HTTPS), check `gh auth status`
- [ ] Go ≥ 1.23 (`go version`)
- [ ] Node LTS + pnpm (`corepack enable && corepack prepare pnpm@latest --activate`)
- [ ] OpenCode installed, then `opencode auth login` → choose **OpenRouter** → paste key #1
- [ ] OpenRouter: create **2 API keys** with credit limits
  - Key #1 `opencode-dev` (for the coding agent) — limit e.g. $3
  - Key #2 `talent-scout-app` (for the app on Railway) — limit e.g. $2
- [ ] Claude (Claude Code / app) ready for the client repo
- [ ] (Optional) **Resend** account (free) + API key — not required for the MVP, default `EMAIL_MODE=log`
- [ ] **Railway** Hobby ($5) + `railway login` (optional CLI)
- [ ] **Supabase** account (free) → create project `talent-scout` (nearest region, e.g. Singapore), save the database password
  - Note down: session pooler URL (Connect → Session pooler) → `DATABASE_URL`; Project URL → `SUPABASE_URL`; `service_role` key → `SUPABASE_SERVICE_ROLE_KEY` (secret, server only)
  - Storage → create a **private** bucket `cvs`
  - Never share the anon/service keys with the client repo or Vercel
- [ ] **Vercel** Hobby account, sign in with GitHub, import the `talent-scout-client` repo, try the project name `talentscout` (→ `talentscout.vercel.app`; if taken, pick another name and let me know so `PUBLIC_APP_URL`/`FRONTEND_ORIGIN` get adjusted)
- [ ] Generate one random `BFF_SHARED_SECRET` (e.g. `openssl rand -base64 32`) and set the **same value** in the server `.env`/Railway Variables and the client `.env`/Vercel env (private, never `PUBLIC_*`)
- [ ] (Optional, post-MVP) buy a domain e.g. `talentscout.com` → point it to Vercel (root + `apply.`) and verify it in Resend
- [ ] Create `.env` from `.env.example` in the server — **don't commit it**
- [ ] Create the worktree folder: `mkdir -p ~/projects/talent-scout/wt` — each issue is worked on in `wt/server-<n>` / `wt/client-<n>` (outside both repos, so no `.gitignore` needed); agents never run in the main checkout (see `WORKFLOW.md` §11)
- [ ] Create GitHub labels & issues (already created by Grok Bot, see `WORKFLOW.md` §8) and enable branch protection on `main` (PR + green CI required)

## 6. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| **Prompt tuning time (main risk)** — Planner/Judge/Scorer need several iterations to produce valid JSON, sensible follow-ups, and evidence quotes that pass the check | All logic is tested first with `LLM_MODE=fake`; prompts in separate files (`internal/llm/prompts/`); Day 4 dedicated to tuning + Day 7 buffer; limit tuning to 1 seeded job & 2–3 sample CVs; record findings in `docs/LESSONS.md`. |
| Resend free without a verified domain can only send to the account owner's email | The MVP doesn't depend on email: `EMAIL_MODE=log` + the "Get invite link" button in the dashboard. |
| Main model errors/rate limits (no fallback in the MVP) | 1× retry on the same model, then `node.failed` + an `error` event; the model can be swapped via `config/models.yaml` without code changes. |
| LLM output is not valid JSON | All structured output is validated by code nodes; planner retries 1×, judge defaults to `sufficient`, scorer retries max 3. |
| FE/BE contract drift | Contract-first, diff check, conformance tests. |
| Supabase tables without RLS can be read/changed by anyone via the anon key (Advisor: critical) | RLS on all tables from the first migration, no public policies; revoke `anon`/`authenticated`; the client never talks to Supabase; check the Advisor before deploy. |
| Supabase free **pauses after 7 days of inactivity** | Open the dashboard & make sure it's *Active* before a demo/interview (restore takes a few minutes); optional weekly ping to `GET /api/v1/health?deep=1`. |
| Free-tier pooler connection limit | `pgxpool` max 5 connections; use the session pooler (IPv4), not the direct connection (IPv6). |
| Subdomain `apply.talentscout.vercel.app` is not possible (Vercel free is single-level only) | One app routed by path (`/apply`, `/s`, `/dashboard`); `apply.` only if a custom domain is bought. |
| Service role key leak | Only in Railway Variables/the server `.env`; never in logs, the client, or Vercel. |
| Coding agent costs balloon | Credit limit on OpenRouter key #1; 1 issue = 1 small PR; restart the agent session every 2–4 issues. |
| Agent stuck / endless fix loop | Max 3 fix attempts per issue → `status:blocked` + comment, move to another issue (`WORKFLOW.md` §4). |
| 1 week isn't enough | Scope already cut (§3); extra cut order in §3; Day 7 is pure buffer. |
| EventSource can't send auth headers | Recruiter run SSE uses a 60 s run-scoped `?sse_token=` minted server-side; the 12 h token never leaves the httpOnly cookie (see `docs/API.md`). |
| Vercel Hobby function limits (300 s max duration, 4.5 MB body) | SSE goes browser → Go directly, never through a function; CV ≤ 2 MB fits the form action. |
| Apply rate limit sees only Vercel IPs behind the BFF | SvelteKit forwards `X-Forwarded-For` + `X-BFF-Secret`; Go trusts XFF only when the secret matches `BFF_SHARED_SECRET`. |

## 7. Out of Scope (MVP)

Auth beyond the single recruiter password + bearer token; multi-tenant/multi-company; Ollama/local LLM (after the MVP); payments; voice/video interviews; CV parsing beyond text PDFs; full i18n (UI in English or Indonesian only, pick one); email notifications to the recruiter; Supabase Auth / Realtime / direct Supabase access from the client.

**Post-MVP** (cut for the 1-week build, see `docs/PRD.md` §6): job create/edit UI & endpoints, Resend as default + custom domain, `:free` model fallback & advanced retries, resuming the runner after a server restart, a full Playwright suite, dark mode.

## 8. Document Map

```
talent-scout/
├── PLAN.md                      ← this document
├── WORKFLOW.md                  ← agentic workflow: labels, issue graph, work loop, learning loop, CI, issue list
├── design-refs/                 ← design references not chosen (not in the repos)
├── talent-scout-server/
│   ├── AGENTS.md                ← OpenCode brief (+ workflow rules)
│   ├── PLAN.md                  ← 7-day backend checklist
│   ├── .opencode/skills/        ← issue-workflow, add-endpoint-from-contract, migration-with-rls
│   ├── .github/                 ← ISSUE_TEMPLATE/task.md, pull_request_template.md
│   └── docs/
│       ├── PRD.md               ← (copied to the client)
│       ├── TECH-STACK.md
│       ├── ARCHITECTURE.md
│       ├── API.md               ← REST contract (copied to the client)
│       ├── EVENTS.md            ← SSE contract (copied to the client)
│       └── LESSONS.md           ← agent lessons log (learning loop)
└── talent-scout-client/
    ├── CLAUDE.md                ← Claude brief (+ workflow rules)
    ├── DESIGN.md                ← design system (Refero "Relate"), assets in docs/design-refs/
    ├── PLAN.md                  ← 7-day frontend checklist
    ├── .claude/skills/          ← issue-workflow, new-page-with-mock, use-design-tokens
    ├── .github/                 ← ISSUE_TEMPLATE/task.md, pull_request_template.md
    └── docs/ PRD.md, API.md, EVENTS.md (identical), LESSONS.md, SERVER-SIDE.md (BFF design, approved), design-refs/
```
