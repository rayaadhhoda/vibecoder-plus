# vibecoder+ — Architecture & Code Guide

A Telegram bot that plans, codes, tests, and opens PRs for any GitHub repo.
Point it at a codebase, describe what you want, and it handles the rest —
asking for clarification when it's uncertain and checking in before touching any code.

---

## How it works (30-second version)

```
You (Telegram)
     │
     ▼
  bot/          ← classifies your message, checks for pending tasks
     │  Redis list (devbot:orch)
     ▼
  orchestrator/ ← fetches repo tree, asks DeepSeek to plan, shows you the plan
     │  Redis list (devbot:worker)  [only after you approve]
     ▼
  worker/       ← clones repo, asks DeepSeek to write code, runs tests, opens PR
     │
     ▼
You (Telegram)  ← gets the PR link (or failure context + retry prompt)
```

Five Docker containers talk to each other only through Redis lists.
No shared filesystem, no direct calls between services.

---

## Services

### `bot/` — I/O layer

Receives Telegram messages, classifies intent, and pushes tasks to the orchestrator queue.
Sends no replies of its own except the initial acknowledgement.

**Key logic in `bot/main.py`:**

```
handle_message()
  │
  ├─ is_chat_mode? ──► push CHAT task, send typing indicator
  │
  ├─ get_pending? ───► push FEEDBACK task (user is replying to a plan/question)
  │
  └─ _classify() ────► push task with detected type, send "Got it [id]"
```

`_classify()` uses keyword matching in priority order:
1. Explicit phrases (`deploy to staging`, `merge PR`, `check staging UI`)
2. First action verb found (`add`, `fix`, `implement`, `deploy`, `merge`, …)
3. Trailing `?` or question starters → STATUS
4. Fallback → CODE

**Commands:**

| Command | Effect |
|---|---|
| `/start` | Show help |
| `/chat` | Enter persistent DeepSeek chat mode |
| `/done` | Exit chat mode, clear conversation history |
| `/cancel <id>` | Set cancel flag for a running task |
| `/queue` | Show pending queue lengths |

---

### `orchestrator/` — The brain

Pops from `devbot:orch`, enriches tasks, and either answers directly (STATUS, CHAT)
or pushes to `devbot:worker` — but only after the user approves the plan.

**Task routing in `orchestrator/main.py`:**

```
process(task)
  │
  ├─ FEEDBACK ──► _handle_feedback()   user replied to a pending task
  ├─ CHAT ──────► _handle_chat()       direct DeepSeek conversation
  ├─ STATUS ────► _handle_status()     GitHub data + DeepSeek answer
  ├─ CODE ──────► plan_task() → _show_plan_and_wait() or ask clarification
  └─ everything else → push to worker queue
```

**Interactive planning flow (CODE tasks):**

```
plan_task()
    │
    ├─ plan["clarification_needed"] is set?
    │       └─ send question to user
    │          set_pending(AWAITING_CLARIFICATION)
    │          stop — wait for reply
    │
    └─ plan is ready
            └─ send numbered plan + file list to user
               set_pending(AWAITING_APPROVAL)
               stop — wait for reply
```

**Feedback handler (`_handle_feedback`):**

```
pending state?
  │
  ├─ cancel word? ──────────────────► "🚫 Cancelled"
  │
  ├─ AWAITING_APPROVAL + yes word? ► push task to worker, clear pending
  │
  └─ anything else ────────────────► append user's words to prompt
                                      replan (loops back through planning flow)
                                      (failure_context also prepended if AWAITING_RETRY)
```

**Planning prompt** asks DeepSeek to return JSON with:
- `summary`, `branch`, `steps`, `files_likely_touched`, `test_command`
- `confidence`: `"high"` / `"medium"` / `"low"`
- `clarification_needed`: a question string, or `null`

All DeepSeek calls use the `@retry(max_attempts=3)` decorator from `shared/retry.py`
so transient API errors don't silently drop tasks.

---

### `worker/` — Execution

Two replicas pop from `devbot:worker` concurrently using `blpop`.
Each task runs in a `ThreadPoolExecutor` with a 10-minute hard timeout.

**Task handlers:**

| Type | What happens |
|---|---|
| `CODE` | `coding.implement()` — worktree, DeepSeek, apply, test, PR |
| `TEST` | `go test` (or custom command from plan) |
| `DEPLOY` | Updates kustomization.yaml image tags, opens promotion PR |
| `PR` | `gh pr` commands (list, view, merge, comments, checks) |
| `UI_CHECK` | Playwright screenshot + Gemini Vision analysis |

**CODE failure handling:**

On failure the worker:
1. Sends a structured failure message: what broke + last 600 chars of test output
2. Calls `q.set_pending(AWAITING_RETRY, task, failure_context=...)` in Redis
3. Next message from the user is routed as FEEDBACK → orchestrator re-plans
   with the failure context + the user's guidance appended to the prompt

**`coding.py` — the coding loop:**

```python
implement(task_id, plan, prompt)
  1. _ensure_base_repo()          clone if first run
  2. git worktree add             isolated branch per task
  3. _check_cancelled()           bail early if /cancel was sent
  4. _read_files(files_to_touch)  give DeepSeek the current content
  5. _check_cancelled()
  6. _ask_deepseek()              returns list of {path, content} dicts
  7. _check_cancelled()
  8. write files to worktree
  9. _check_cancelled()
 10. _run_tests()                 go test (or plan's test_command)
 11. git add -A && git commit
 12. git push + gh pr create
```

DeepSeek returns code using `<<<FILE: path>>>` / `<<<END>>>` delimiters
instead of JSON — avoids JSON-escaping issues with multiline code.

Cancel checks happen between every major step so `/cancel <id>` is
responsive even mid-implementation.

---

### `shared/` — Shared primitives

#### `models.py`

```python
class TaskType(str, Enum):
    CODE     # implement a feature / fix a bug
    TEST     # run the test suite
    UI_CHECK # visual validation via Gemini
    DEPLOY   # update kustomization.yaml image tags, open PR
    PR       # gh pr list / view / merge / comments / checks
    STATUS   # one-shot GitHub data lookup + DeepSeek answer
    CHAT     # persistent multi-turn DeepSeek conversation
    FEEDBACK # user's reply to a pending plan/question/failure

class TaskStatus(str, Enum):
    QUEUED
    IN_PROGRESS
    DONE
    FAILED
    AWAITING_CLARIFICATION  # orchestrator asked a question
    AWAITING_APPROVAL       # plan shown, waiting for go-ahead
    AWAITING_RETRY          # worker failed, waiting for guidance
```

`Task` is a plain dataclass with `to_dict` / `from_dict` for Redis serialisation.

#### `queue.py`

All inter-service state lives in Redis. The `Queue` class is a thin wrapper.

**Redis key map:**

| Key | Type | TTL | Purpose |
|---|---|---|---|
| `devbot:orch` | list | — | Bot → orchestrator queue |
| `devbot:worker` | list | — | Orchestrator → worker queue |
| `devbot:cancel:{task_id}` | string | 1h | Cancel flag set by `/cancel` |
| `devbot:session:{user_id}` | string (JSON) | 1h | Last task ID / type per user |
| `devbot:chat:{user_id}` | string | — | Chat mode flag (no TTL — persists until `/done`) |
| `devbot:conv:{user_id}` | string (JSON) | 24h | DeepSeek conversation history (max 40 turns) |
| `devbot:pending:{user_id}` | string (JSON) | 1h | Pending task + state (approval / clarification / retry) |

**Pending task payload:**
```json
{
  "state": "awaiting_approval | awaiting_clarification | awaiting_retry",
  "task": { ...Task.to_dict()... },
  "failure_context": "optional — set by worker on CODE failure"
}
```

#### `retry.py`

```python
@retry(max_attempts=3, base_delay=2.0)
def some_api_call(): ...
```

Exponential backoff: 2s → 4s → 8s. Used on all DeepSeek calls in both
the orchestrator and coding worker.

---

## Full message lifecycle (CODE task)

```
1.  User:         "add pagination to the agents list endpoint"
2.  bot:          classify → CODE, push to devbot:orch, "Got it [a1b2c3]"
3.  orchestrator: pop devbot:orch
                  fetch GitHub file tree (API)
                  ask DeepSeek to plan → {summary, steps, files, confidence: "high"}
4.  orchestrator: send plan to user:
                    📋 Plan: Add cursor-based pagination to GET /agents
                    1. Add page/limit params to handler
                    2. Update store query
                    Files: `internal/agents/handler.go`, `internal/agents/store.go`
                    Reply yes to proceed, give feedback to adjust, or cancel.
                  set_pending(AWAITING_APPROVAL, task)

5.  User:         "yes but also add it to the frontend table"
6.  bot:          get_pending → found, push FEEDBACK task
7.  orchestrator: _handle_feedback → not a yes, re-plan with feedback appended
                  plan again with "User says: also add it to the frontend table"
                  send updated plan
                  set_pending(AWAITING_APPROVAL, task)

8.  User:         "yes"
9.  bot:          get_pending → found, push FEEDBACK task
10. orchestrator: _handle_feedback → positive → push to devbot:worker

11. worker:       pop devbot:worker
                  git worktree add feature/add-pagination-agents /worktrees/task-a1b2c3
                  read handler.go, store.go, PaginatedTable.tsx
                  ask DeepSeek → writes full file contents
                  apply files, run go test + npm test
                  git commit + push + gh pr create
12. worker:       send result:
                    ✅ Done [a1b2c3]
                    Add cursor-based pagination to agents list
                    PR: https://github.com/org/repo/pull/42
                    Files: internal/agents/handler.go, ...
                    Tests: ok ./internal/agents/... (0.4s)
```

---

## Git isolation

Each CODE or DEPLOY task gets its own git worktree:

```
/worktrees/
  base/              ← permanent clone (seeded on first run)
  task-a1b2c3/       ← worktree for task a1b2c3 (deleted after)
  task-b2c3d4/       ← worktree for task b2c3d4 (deleted after)
  deploy-e3f4g5/     ← worktree for a staging promotion
```

Worktrees are created from `origin/main` each time, so tasks always start
from a clean base regardless of what other tasks are doing in parallel.
Stale worktrees and remote branches from prior failed runs are force-deleted
before creating new ones so retries always start clean.

---

## Chat mode

`/chat` enters a persistent conversational mode with DeepSeek:

- All messages bypass the task classifier and go straight to `_handle_chat()`
  in the orchestrator — no workers, no queues, immediate response
- Conversation history is stored in `devbot:conv:{user_id}` (Redis, 24h TTL, 40-turn window)
- History is passed as the `messages` array on every call so DeepSeek has full context
- `/done` clears the history and returns to normal bot mode

---

## Configuration (`.env`)

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | From BotFather |
| `ALLOWED_TELEGRAM_IDS` | yes | Comma-separated Telegram user IDs |
| `GITHUB_REPO` | yes | `owner/repo` |
| `GITHUB_TOKEN` | yes | PAT with `repo` + `workflow` scopes |
| `DEEPSEEK_API_KEY` | yes | From platform.deepseek.com |
| `DEEPSEEK_MODEL` | no | Default: `deepseek-chat` |
| `REDIS_URL` | no | Default: `redis://redis:6379` (docker-compose service name) |
| `REPO_PATH` | no | Default: `/worktrees/base` |
| `DEVBOT_GIT_EMAIL` | no | Default: `devbot@example.com` |
| `DEVBOT_GIT_NAME` | no | Default: `DevBot` |
| `STAGING_URL` | no | Required for UI check commands |
| `GOOGLE_CLOUD_PROJECT` | no | Required for UI check commands (Gemini Vision) |
| `GOOGLE_CLOUD_REGION` | no | Default: `us-central1` |
| `GEMINI_MODEL` | no | Default: `gemini-2.0-flash` |

---

## Docker volumes

| Volume | Mounted in | Purpose |
|---|---|---|
| `redis_data` | redis | Persist queue and session state across restarts |
| `worktrees` | orchestrator, worker | Shared git worktree space |
| `go_cache` | worker | Go module cache — survives container restarts |
| `go_build_cache` | worker | Go build cache — keeps test runs fast |

The Go module cache is also pre-warmed in a background thread on worker startup
(`go mod download`) so the first test after a cold deploy isn't slow.

---

## Adding a new task type

1. Add a value to `TaskType` in `shared/models.py`
2. Add keyword detection in `_classify()` in `bot/main.py`
3. Add a handler function in `worker/main.py` (or `orchestrator/main.py` for
   tasks that don't need the full coding pipeline)
4. Wire it in `process()` / `handle()`

Non-coding tasks (STATUS, CHAT, FEEDBACK) are handled directly in the orchestrator.
Anything that needs git, tests, or PRs goes through the worker.
