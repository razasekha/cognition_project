# Devin Three-Agent Remediation Automation

An event-driven orchestration system that uses the [Devin API](https://docs.devin.ai/api-reference/overview) to automate code quality enforcement on a GitHub repository. Three Devin agents collaborate:

| Agent | Role | Trigger |
|-------|------|---------|
| **Agent 1 – Injector** | Introduces a realistic flaw (bad engineer simulation) | `POST /inject` |
| **Agent 2 – Scanner** | Scans the repo, logs findings, opens GitHub issues | Hourly cron + `POST /scan` |
| **Agent 3 – Fixer** | Remediates each finding and opens a pull request | Auto-fired by Agent 2 + `POST /fix/{id}` |

## Architecture

```
POST /inject {focus_area}
        │
        ▼
  Agent 1 (Devin)  ─── merges flaw ──► razasekha/superset-devin (GitHub)
                                                │
                             APScheduler hourly ▼
                         Agent 2 (Devin, structured_output)
                                                │
                              findings JSON ────▼
                         SQLite issues log + GitHub issues
                                                │
                           new finding ─────────▼
                         Agent 3 (Devin)  ──── PR ──► GitHub
                                                │
                                       GET /dashboard
```

## Prerequisites

1. A Devin account with a service user API key (`cog_` prefix)
2. Devin's GitHub app connected and granted write access to the target fork
3. A GitHub personal access token with `repo` scope on the fork
4. Docker and Docker Compose installed

## Setup

```bash
# 1. Clone this repo
git clone https://github.com/YOUR_ORG/devin-orchestrator
cd devin-orchestrator

# 2. Configure environment
cp .env.example .env
# Edit .env with your keys (see below)

# 3. Build and start
docker compose up --build
```

## Environment variables

| Variable | Description |
|----------|-------------|
| `DEVIN_API_KEY` | Service user key (`cog_...`) |
| `DEVIN_ORG_ID` | Devin organization ID (`org-...`) |
| `TARGET_REPO` | GitHub `owner/repo` to operate on |
| `SCAN_CRON` | Cron expression for Agent 2 (default: `0 * * * *`) |
| `MAX_ACU_INJECTOR` | ACU spend cap for Agent 1 sessions |
| `MAX_ACU_SCANNER` | ACU spend cap for Agent 2 sessions |
| `MAX_ACU_FIXER` | ACU spend cap for Agent 3 sessions |
| `POLL_INTERVAL_SECONDS` | How often to poll session status (default: 15) |
| `POLL_TIMEOUT_SECONDS` | Max wait for a session to finish (default: 3600) |
| `GITHUB_TOKEN` | GitHub PAT for opening issues |
| `GITHUB_REPO` | GitHub `owner/repo` for issue creation |

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/inject` | Trigger Agent 1 with `{"focus_area": "security"}` |
| `POST` | `/scan` | Trigger Agent 2 scan immediately |
| `POST` | `/fix/{issue_id}` | Trigger Agent 3 for a specific issue |
| `GET`  | `/dashboard` | HTML observability dashboard |
| `GET`  | `/api/issues` | JSON list of all logged issues |
| `GET`  | `/api/sessions` | JSON list of all Devin sessions |
| `GET`  | `/api/metrics` | JSON summary metrics |
| `GET`  | `/healthz` | Health check |

## Simulating the workflow (end-to-end demo)

```bash
# Step 1: Inject a flaw into the repo
curl -X POST http://localhost:8000/inject \
  -H "Content-Type: application/json" \
  -d '{"focus_area": "security - introduce a hardcoded API key or SQL injection vulnerability"}'

# Step 2: Trigger a scan immediately (or wait for the hourly cron)
curl -X POST http://localhost:8000/scan

# Step 3: Watch the dashboard
open http://localhost:8000/dashboard

# Step 4: Manually fix a specific issue (optional - Agent 3 auto-fires after Agent 2)
curl -X POST http://localhost:8000/fix/1
```

## Observability

The `/dashboard` answers the "how would a leader know this is working?" question:

- **Issues found vs fixed** over time
- **Success rate** for remediation sessions
- **Mean time to fix** (MTTR)
- **Pull request links** with status
- **ACU spend** per agent and total
- **Active sessions** with live Devin links

## Loom talking points

**What:** Engineering teams accumulate tech debt and vulnerabilities faster than they can remediate them. This system creates a continuous remediation loop — find, log, fix — that runs autonomously, without human scheduling or coordination.

**How:** Three Devin agents orchestrated via the v3 API. Agent 1 simulates a developer merging a flawed change. Agent 2 scans the repo on a cron schedule and uses `structured_output_schema` to return machine-readable findings that feed directly into Agent 3's fix sessions. Each session is tagged (`agent:scanner`, `run:<uuid>`) for correlation. The SQLite log is the source of truth; the dashboard is the engineering leader's view.

**Why Devin:** A traditional linter or SAST tool finds issues but can't fix them. Devin closes the loop — it understands context, writes code, opens PRs with descriptions, and can handle ambiguous or multi-file fixes. The `structured_output_schema` is what makes the agent-to-agent handoff reliable: JSON in, JSON out, no prompt parsing.

**When (next steps):** Replace the hourly cron with a GitHub webhook (push events), swap SQLite for Postgres/Jira, add Slack notifications via `PATCH /sessions/{id}/messages`, and use Devin's native scheduled sessions endpoint for the scanner once it supports `structured_output_schema`.

## Key design decisions

- `bypass_approval: true` on all autonomous sessions — avoids approval gates blocking the demo loop
- Per-role `max_acu_limit` — cost-control guardrail (good story for VPs)
- Fingerprint dedup (`sha1(file|category|line)`) before firing Agent 3 — no duplicate PRs per scan cycle
- Agent 1 merges to `master` directly (fork has no branch protection) — keeps the demo simple
