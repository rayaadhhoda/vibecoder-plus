# vibecoder+

A Telegram bot that codes for you. Point it at any GitHub repo, describe a feature or bug fix in plain English, and it plans, implements, tests, and opens a PR — hands-off.

```
you: fix the policy active field to reflect paused_until state
bot: Planning...
bot: Plan: Fix policy active field to reflect paused_until future state
     Files: cmd/control-plane/routes.go

     [~45 seconds later]

bot: Done [a1b2c3d4]
     Set active to false in policy responses when paused_until is in the future
     PR: https://github.com/yourorg/yourrepo/pull/42
     Files: cmd/control-plane/routes.go
```

## How it works

```
Telegram → Bot → Orchestrator → Worker
                    │               │
                    │               ├── DeepSeek (code generation)
                    │               ├── git worktree (isolated branch)
                    │               ├── go test / npm test
                    │               └── gh pr create
                    │
                    └── DeepSeek (planning + repo tree from GitHub API)
```

Five services, all in Docker:

| Service | Role |
|---|---|
| **bot** | Receives Telegram messages, classifies intent, pushes to queue |
| **orchestrator** | Fetches real repo file tree, asks DeepSeek to make a plan, forwards to workers |
| **worker** (×2) | Clones repo, asks DeepSeek to implement, commits + pushes + opens PR |
| **redis** | Queue between services + session state |

## Commands

Just talk to it naturally. Examples:

| Message | What happens |
|---|---|
| `fix the login timeout bug` | Full code → commit → PR |
| `add pagination to the users API` | Same |
| `run tests` | Runs scoped unit tests, sends results |
| `what PRs are open` | Live GitHub data, answers immediately |
| `comments on PR #42` | Shows PR review comments |
| `merge PR #42` | `gh pr merge --squash --auto` |
| `deploy to staging` | Updates k8s image tags, opens promotion PR |
| `check staging UI /dashboard` | Playwright screenshot + Gemini Vision analysis |
| Send a screenshot | Compares against staging via Gemini |

## Setup

### 1. Prerequisites

- Docker + Docker Compose
- A Telegram bot token ([BotFather](https://t.me/botfather))
- A GitHub personal access token (repo + workflow scopes)
- A DeepSeek API key ([platform.deepseek.com](https://platform.deepseek.com))
- Your Telegram user ID ([userinfobot](https://t.me/userinfobot))

### 2. Configure

```bash
cp .env.example .env
# Edit .env with your values
```

Required:
- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_TELEGRAM_IDS` — your Telegram user ID(s)
- `GITHUB_REPO` — `owner/repo`
- `GITHUB_TOKEN`
- `DEEPSEEK_API_KEY`

Optional (UI validation only):
- `STAGING_URL`, `GOOGLE_CLOUD_PROJECT`, `GEMINI_MODEL`

### 3. Run

```bash
docker compose up -d
```

First run clones the repo into a persistent Docker volume. The Go module cache also persists across restarts so test runs stay fast.

### 4. Talk to the bot

Find your bot on Telegram and send `/start`. That's it.

## Architecture notes

**Queue separation** — the bot pushes to `devbot:orch` (orchestrator queue) and workers pop from `devbot:worker`. This prevents unplanned tasks reaching workers before the orchestrator adds an execution plan.

**Git worktrees** — each coding task gets an isolated `git worktree` at `/worktrees/task-{id}`. Multiple tasks can run in parallel without conflicting. Worktrees are cleaned up after each task.

**Stale branch cleanup** — before creating a worktree, the worker deletes any stale local branch and remote branch with the same name. Retrying a failed task always starts clean.

**Response format** — DeepSeek returns code using `<<<FILE: path>>>` / `<<<END>>>` delimiters instead of JSON, avoiding JSON escaping issues with multiline code.

**Go module cache** — pre-warmed in a background thread on worker startup, persisted in a named Docker volume. First test after a fresh deploy is instant.

## Customising

The orchestrator's planning prompt includes the full repo file tree (fetched from GitHub API) so DeepSeek picks real file paths. The default test command runs `./internal/policy/... ./pkg/types/...` — update `PLANNING_SYSTEM_PROMPT` in `orchestrator/main.py` and the default in `worker/main.py` to match your project's test structure.

For non-Go projects, swap the test command and remove the Go Dockerfile steps.

## License

MIT
