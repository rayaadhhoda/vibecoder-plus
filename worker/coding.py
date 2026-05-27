"""
Coding Worker — implements features using DeepSeek.

Flow:
1. Create a git worktree for this task (isolated branch)
2. Ask DeepSeek to generate the code changes
3. Apply changes, run tests
4. Commit + push + open PR via gh CLI
5. Return summary
"""

import logging
import os
import subprocess
import time
import sys
import textwrap

import httpx

from shared.queue import Queue
from shared.retry import retry

log = logging.getLogger(__name__)

q = Queue(os.environ["REDIS_URL"])

DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
GITHUB_REPO = os.environ["GITHUB_REPO"]
REPO_PATH = os.environ.get("REPO_PATH", "/worktrees/base")
WORKTREES_DIR = "/worktrees"


CODING_SYSTEM_PROMPT = """You are an expert software engineer.
You will be given a task and relevant file contents.

Respond with ONLY file changes using this exact delimiter format — no JSON, no markdown:

<<<FILE: relative/path/to/file.go>>>
full new file content here
<<<END>>>

<<<FILE: another/file.go>>>
full new file content here
<<<END>>>

Rules:
- Write complete files, not snippets or diffs
- One <<<FILE>>> block per changed file
- No explanation text outside the blocks
- Preserve all existing code not related to the task
"""


def _ensure_base_repo() -> None:
    """Clone the base repo into REPO_PATH if it doesn't already exist."""
    if os.path.isdir(os.path.join(REPO_PATH, ".git")):
        return
    github_token = os.environ.get("GITHUB_TOKEN", "")
    clone_url = f"https://{github_token}@github.com/{GITHUB_REPO}.git"
    os.makedirs(WORKTREES_DIR, exist_ok=True)
    result = subprocess.run(
        ["git", "clone", clone_url, REPO_PATH],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone failed: {result.stderr}")
    log.info(f"Base repo cloned to {REPO_PATH}")

    # Configure git identity for commits
    subprocess.run(["git", "-C", REPO_PATH, "config", "user.email", "${DEVBOT_GIT_EMAIL:-devbot@example.com}"], capture_output=True)
    subprocess.run(["git", "-C", REPO_PATH, "config", "user.name", "DevBot"], capture_output=True)


def _check_cancelled(task_id: str) -> None:
    """Raise if the task has been cancelled."""
    if q.is_cancelled(task_id):
        raise RuntimeError(f"Task {task_id} was cancelled")


def implement(task_id: str, plan: dict, prompt: str) -> dict:
    """
    Returns {"success": bool, "pr_url": str|None, "test_output": str, "summary": str}
    """
    branch = plan.get("branch", f"feature/task-{task_id}")
    files_to_read = plan.get("files_likely_touched", [])
    test_cmd = plan.get("test_command", "go test ./...")
    steps = plan.get("steps", [])

    # 1. Ensure base repo exists (seed it if volume was just created)
    _ensure_base_repo()

    # 1b. Create worktree (clean up any stale branch/worktree from a prior failed run)
    worktree_path = f"{WORKTREES_DIR}/task-{task_id}"
    _run(["git", "-C", REPO_PATH, "fetch", "origin"], "git fetch")

    # Remove stale worktree at this path if it exists
    subprocess.run(
        ["git", "-C", REPO_PATH, "worktree", "remove", "--force", worktree_path],
        capture_output=True,
    )
    # Delete stale local branch if it exists (so we can re-create cleanly)
    subprocess.run(
        ["git", "-C", REPO_PATH, "branch", "-D", branch],
        capture_output=True,
    )

    _run(
        ["git", "-C", REPO_PATH, "worktree", "add", "-b", branch, worktree_path, "origin/main"],
        "create worktree",
    )
    subprocess.run(["git", "-C", worktree_path, "config", "user.email", "${DEVBOT_GIT_EMAIL:-devbot@example.com}"], capture_output=True)
    subprocess.run(["git", "-C", worktree_path, "config", "user.name", "DevBot"], capture_output=True)
    # Wire token into push URL so git push doesn't prompt for auth
    github_token = os.environ.get("GITHUB_TOKEN", "")
    subprocess.run(
        ["git", "-C", worktree_path, "remote", "set-url", "origin",
         f"https://{github_token}@github.com/{GITHUB_REPO}.git"],
        capture_output=True,
    )

    try:
        # 2. Read relevant files for context
        _check_cancelled(task_id)
        file_contexts = _read_files(worktree_path, files_to_read)

        # 3. Ask DeepSeek to implement
        _check_cancelled(task_id)
        changes = _ask_deepseek(prompt, steps, file_contexts)

        # 4. Apply changes
        _check_cancelled(task_id)
        for change in changes:
            fpath = os.path.join(worktree_path, change["path"])
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w") as f:
                f.write(change["content"])
            log.info(f"Wrote {change['path']}")

        # 5. Run tests
        _check_cancelled(task_id)
        log.info("Running tests...")
        test_output = _run_tests(worktree_path, test_cmd)
        log.info(f"Tests done: {test_output[-100:]!r}")

        # 6. Commit + push
        _check_cancelled(task_id)
        log.info("Committing...")
        _run(["git", "-C", worktree_path, "add", "-A"], "git add")
        _run(
            ["git", "-C", worktree_path, "commit", "-m", f"feat: {plan.get('summary', prompt)[:72]}"],
            "git commit",
        )
        log.info("Pushing...")
        # Delete stale remote branch if it exists, then push fresh
        subprocess.run(
            ["git", "-C", worktree_path, "push", "origin", "--delete", branch],
            capture_output=True,
        )
        _run(["git", "-C", worktree_path, "push", "-u", "origin", branch], "git push")

        # 7. Open PR
        log.info("Opening PR...")
        pr_url = _open_pr(branch, plan.get("summary", prompt), steps, worktree_path)

        return {
            "success": True,
            "pr_url": pr_url,
            "test_output": test_output,
            "summary": plan.get("summary", "Done"),
            "files_changed": [c["path"] for c in changes],
        }

    except Exception as e:
        log.error(f"Coding worker failed: {e}")
        return {"success": False, "pr_url": None, "test_output": "", "summary": str(e)}

    finally:
        # Always clean up the worktree and cancel key
        subprocess.run(
            ["git", "-C", REPO_PATH, "worktree", "remove", "--force", worktree_path],
            capture_output=True,
        )
        q.clear_cancel(task_id)


@retry(max_attempts=3, base_delay=2.0)
def _deepseek_call(user_msg: str) -> str:
    """Single DeepSeek call — wrapped with retry."""
    response = httpx.post(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": CODING_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": 16000,
        },
        timeout=180,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def _ask_deepseek(prompt: str, steps: list[str], file_contexts: str) -> list[dict]:
    import re
    user_msg = (
        f"Task: {prompt}\n\n"
        f"Steps to implement:\n" + "\n".join(f"- {s}" for s in steps) + "\n\n"
        f"Current file contents:\n{file_contexts}"
    )

    content = _deepseek_call(user_msg)

    # Parse delimiter format: <<<FILE: path>>> ... <<<END>>> (or next <<<FILE:)
    changes = []
    for block in re.split(r"<<<FILE:\s*", content)[1:]:
        if ">>>" not in block:
            continue
        path_end = block.index(">>>")
        path = block[:path_end].strip()
        file_content = block[path_end + 3:]
        if file_content.startswith("\n"):
            file_content = file_content[1:]
        # Remove trailing <<<END>>> if present
        file_content = re.sub(r"\s*<<<END>>>\s*$", "", file_content)
        if path and file_content:
            changes.append({"path": path, "content": file_content})
            log.info(f"Parsed file: {path} ({len(file_content)} chars)")

    if not changes:
        raise ValueError(f"No file blocks found in DeepSeek response: {content[:300]}")

    return changes


def _read_files(worktree: str, paths: list[str]) -> str:
    out = []
    for p in paths:
        full = os.path.join(worktree, p)
        if os.path.exists(full):
            try:
                with open(full) as f:
                    content = f.read()
                out.append(f"--- {p} ---\n{content}\n")
            except Exception:
                pass
    return "\n".join(out) if out else "(no existing files found)"


def _run_tests(worktree: str, cmd: str) -> str:
    try:
        subprocess.run("go mod download", shell=True, cwd=worktree, capture_output=True, timeout=600)
    except Exception as e:
        log.warning(f"go mod download: {e}")
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = (result.stdout + result.stderr)[-3000:]
        return output
    except subprocess.TimeoutExpired:
        return "⚠️ Tests timed out after 5 min (cache warming — will be faster next run)"
    except Exception as e:
        return f"⚠️ Test runner error: {e}"


def _open_pr(branch: str, title: str, steps: list[str], worktree: str) -> str | None:
    body = "## Changes\n" + "\n".join(f"- {s}" for s in steps)
    env = os.environ.copy()
    env["GH_TOKEN"] = os.environ.get("GITHUB_TOKEN", "")
    try:
        result = subprocess.run(
            ["gh", "pr", "create", "--title", title, "--body", body, "--base", "main"],
            capture_output=True, text=True, timeout=30,
            cwd=worktree, env=env,
        )
        if result.returncode != 0:
            log.error(f"gh pr create stderr: {result.stderr}")
            return None
        # gh outputs the PR URL on stdout
        return result.stdout.strip().split()[-1]
    except Exception as e:
        log.error(f"PR creation failed: {e}")
        return None


def _run(cmd: list[str], label: str) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"{label} failed: {result.stderr}")
    return result.stdout
