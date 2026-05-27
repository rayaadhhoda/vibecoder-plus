"""
Orchestrator — the brain.

Pops tasks from the orchestrator queue (ORCH_QUEUE), enriches CODE tasks with
a concrete execution plan (file tree + DeepSeek), answers STATUS questions
with real GitHub data, then forwards everything to the worker queue.
"""

import json
import logging
import os
import sys

sys.path.insert(0, "/app")

import httpx

from shared.models import Task, TaskType, TaskStatus
from shared.queue import Queue
from shared.retry import retry

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

q = Queue(os.environ["REDIS_URL"])

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
GITHUB_REPO = os.environ["GITHUB_REPO"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

PLANNING_SYSTEM_PROMPT = """You are a senior software engineer orchestrating development tasks on the {repo} codebase.
Given a user request and the repository file tree, produce a concise JSON execution plan.

Use the file tree to identify the exact files that need changing — do not guess paths that aren't listed.

Output ONLY valid JSON with this shape:
{{
  "summary": "one-line summary of what will happen",
  "branch": "feature/short-kebab-name",
  "steps": [
    "step 1 description",
    "step 2 description"
  ],
  "files_likely_touched": ["exact/path/from/tree.go", "frontend/src/exact/File.tsx"],
  "test_command": "go test ./internal/policy/... ./pkg/types/... 2>&1 | tail -40",
  "needs_playwright": false
}}
"""


def _get_repo_tree() -> str:
    """Fetch file paths from GitHub API for planning context."""
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/git/trees/HEAD?recursive=1",
            headers=GH_HEADERS,
            timeout=15,
            follow_redirects=True,
        )
        r.raise_for_status()
        tree = r.json().get("tree", [])
        paths = [
            item["path"] for item in tree
            if item["type"] == "blob"
            and not any(item["path"].startswith(x) for x in [
                "node_modules/", "vendor/", "dist/", "build/",
                "sdk/python/build/", ".git/",
            ])
            and not item["path"].endswith((
                ".png", ".jpg", ".jpeg", ".svg", ".ico",
                ".woff", ".woff2", ".ttf", ".eot", ".pdf",
            ))
        ]
        return "\n".join(paths[:600])
    except Exception as e:
        log.warning(f"Could not fetch repo tree: {e}")
        return ""


def _get_repo_context(prompt: str = "") -> dict:
    """Fetch open PRs, recent commits, CI status, and optionally a specific PR for STATUS queries."""
    import re
    ctx = {}

    try:
        prs = httpx.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/pulls?state=open&per_page=10",
            headers=GH_HEADERS, timeout=10, follow_redirects=True,
        ).json()
        ctx["open_prs"] = [
            f"#{p['number']} {p['title']} ({p['head']['ref']}) by {p['user']['login']}"
            for p in prs
        ]
    except Exception:
        ctx["open_prs"] = []

    try:
        commits = httpx.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/commits?per_page=5",
            headers=GH_HEADERS, timeout=10, follow_redirects=True,
        ).json()
        ctx["recent_commits"] = [
            f"{c['sha'][:7]} {c['commit']['message'].splitlines()[0]}"
            for c in commits
        ]
    except Exception:
        ctx["recent_commits"] = []

    try:
        runs = httpx.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?per_page=3",
            headers=GH_HEADERS, timeout=10, follow_redirects=True,
        ).json()
        ctx["ci_runs"] = [
            f"{r['name']} — {r['conclusion'] or r['status']} on {r['head_branch']} ({r['head_sha'][:7]})"
            for r in runs.get("workflow_runs", [])
        ]
    except Exception:
        ctx["ci_runs"] = []

    # If the prompt mentions a specific PR number, fetch that PR's full detail + file list
    m = re.search(r"#?(\d{3,5})", prompt)
    if m:
        pr_num = m.group(1)
        try:
            pr = httpx.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{pr_num}",
                headers=GH_HEADERS, timeout=10,
            ).json()
            state = pr.get("state", "unknown")
            merged = pr.get("merged", False)
            merge_sha = (pr.get("merge_commit_sha") or "")[:7]
            ctx["pr_detail"] = (
                f"#{pr_num} '{pr.get('title')}' — state={state} merged={merged} "
                f"merge_commit={merge_sha} base={pr.get('base', {}).get('ref')} "
                f"head={pr.get('head', {}).get('ref')}"
            )
        except Exception:
            ctx["pr_detail"] = f"(could not fetch PR #{pr_num})"

        try:
            files = httpx.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/pulls/{pr_num}/files?per_page=50",
                headers=GH_HEADERS, timeout=10,
            ).json()
            ctx["pr_files"] = [
                f"{f['status']} {f['filename']} (+{f['additions']} -{f['deletions']})"
                for f in files
            ]
        except Exception:
            ctx["pr_files"] = []

    return ctx


@retry(max_attempts=3, base_delay=2.0)
def _deepseek_chat(messages: list, temperature: float = 0.2, timeout: int = 60, response_format: dict | None = None) -> dict:
    """Call DeepSeek API with retry."""
    body: dict = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format:
        body["response_format"] = response_format
    response = httpx.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        json=body,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def plan_task(task: Task) -> dict:
    session = q.get_session(task.user_id)
    recent_context = session.get("last_result", "")
    tree = _get_repo_tree()

    content = _deepseek_chat(
        messages=[
            {
                "role": "system",
                "content": PLANNING_SYSTEM_PROMPT.format(repo=GITHUB_REPO),
            },
            {
                "role": "user",
                "content": (
                    f"Repository file tree:\n{tree}\n\n"
                    f"Recent session context: {recent_context}\n\n"
                    f"Task: {task.prompt}"
                ),
            },
        ],
        temperature=0.2,
        timeout=60,
        response_format={"type": "json_object"},
    )
    return json.loads(content)


def _handle_status(task: Task) -> None:
    """Answer questions using real GitHub data + DeepSeek."""
    ctx = _get_repo_context(task.prompt)
    session = q.get_session(task.user_id)
    last_result = session.get("last_result", "")

    sections = [
        f"Open PRs:\n" + "\n".join(ctx["open_prs"] or ["(none)"]),
        f"Recent commits:\n" + "\n".join(ctx["recent_commits"] or ["(none)"]),
        f"Recent CI runs:\n" + "\n".join(ctx["ci_runs"] or ["(none)"]),
    ]
    if "pr_detail" in ctx:
        sections.append(f"PR detail:\n{ctx['pr_detail']}")
    if ctx.get("pr_files"):
        sections.append(f"PR files changed:\n" + "\n".join(ctx["pr_files"]))
    sections.append(f"Last devbot result: {last_result or '(none)'}")

    system = (
        f"You are a helpful assistant for the {GITHUB_REPO} codebase. "
        "You have access to live GitHub data shown below. Answer concisely and factually. "
        "Format for Telegram Markdown (use backticks for code/SHAs, *bold* for headings).\n\n"
        + "\n\n".join(sections)
    )

    try:
        content = _deepseek_chat(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": task.prompt},
            ],
            temperature=0.2,
            timeout=30,
        )
        _send(task.chat_id, content)
    except Exception as e:
        log.error(f"Status query failed: {e}")
        _send(task.chat_id, f"❌ Couldn't fetch status: {e}")


def process(task: Task) -> None:
    log.info(f"Orchestrating {task.id} type={task.type}")

    # Check for cancel before doing any work
    if q.is_cancelled(task.id):
        _send(task.chat_id, f"🚫 Task `[{task.id}]` was cancelled before it started.")
        return

    if task.type == TaskType.STATUS:
        _handle_status(task)
        return

    if task.type == TaskType.CODE:
        try:
            _send(task.chat_id, f"🗺 Planning `[{task.id}]`...")
            plan = plan_task(task)
            task.context["plan"] = plan
            log.info(f"Plan for {task.id}: {plan['summary']}")
            files = ", ".join(plan["files_likely_touched"][:5])
            _send(task.chat_id, f"📋 Plan: {plan['summary']}\nFiles: {files}")
        except Exception as e:
            log.error(f"Planning failed: {e}")
            _send(task.chat_id, f"❌ Planning failed: {e}")
            return

    task.status = TaskStatus.QUEUED
    q.push(task)  # → WORKER_QUEUE
    q.update_session(task.user_id, {"last_task_id": task.id, "last_task_type": task.type.value})


def _send(chat_id: int, text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception:
        try:
            httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        except Exception as e:
            log.error(f"Failed to send Telegram message: {e}")


def main() -> None:
    log.info("Orchestrator running...")
    while True:
        task = q.pop_orch(timeout=5)
        if task:
            try:
                process(task)
            except Exception as e:
                log.error(f"Orchestrator error on {task.id}: {e}")


if __name__ == "__main__":
    main()
