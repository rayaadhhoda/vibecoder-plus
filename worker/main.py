"""
Worker — executes tasks dispatched by the orchestrator.
"""

import asyncio
import logging
import os
import re
import subprocess
import sys
import threading

sys.path.insert(0, "/app")

from shared.models import Task, TaskType, TaskStatus
from shared.queue import Queue
import coding
import ui_validator

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Pre-warm Go module cache on startup in a background thread so the first
# test task doesn't have to wait for dependency downloads.
def _prewarm_go_cache():
    try:
        log.info("Pre-warming Go module cache...")
        r = subprocess.run(
            "go mod download",
            shell=True, cwd=coding.REPO_PATH,
            capture_output=True, timeout=600,
        )
        if r.returncode == 0:
            log.info("Go module cache warm.")
        else:
            log.warning(f"go mod download exited {r.returncode}: {r.stderr.decode()[:200]}")
    except Exception as e:
        log.warning(f"Cache pre-warm failed (non-fatal): {e}")

threading.Thread(target=_prewarm_go_cache, daemon=True).start()

q = Queue(os.environ["REDIS_URL"])
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_ENV = {**os.environ, "GH_TOKEN": GITHUB_TOKEN}


def handle(task: Task) -> str:
    # ── Code implementation ──────────────────────────────────────────────────
    if task.type == TaskType.CODE:
        plan = task.context.get("plan", {})
        result = coding.implement(task.id, plan, task.prompt)
        if result["success"]:
            files = ", ".join(result["files_changed"])
            snippet = result["test_output"][-1000:] if result["test_output"] else ""
            return (
                f"Done [{task.id}]\n"
                f"{result['summary']}\n\n"
                f"PR: {result['pr_url']}\n"
                f"Files: {files}\n\n"
                f"Tests:\n{snippet}"
            )
        return f"Failed [{task.id}]\n{result['summary']}"

    # ── Test runner ──────────────────────────────────────────────────────────
    elif task.type == TaskType.TEST:
        cwd = task.context.get("path", coding.REPO_PATH)
        user_cmd = task.context.get("command")
        cmd = user_cmd or "go test ./internal/policy/... ./pkg/types/... ./internal/policyregistry/..."

        # Ensure module cache is warm (background thread may still be running)
        try:
            subprocess.run("go mod download", shell=True, cwd=cwd, capture_output=True, timeout=600)
        except Exception as e:
            log.warning(f"go mod download: {e}")

        try:
            r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=300)
            # combine stdout + stderr; fall back to a clear message if both empty
            output = (r.stdout + r.stderr).strip()
            if not output:
                output = "(no output — all tests cached or nothing ran)"
            output = output[-2000:]
            icon = "✅" if r.returncode == 0 else "❌"
            return f"{icon} Tests [{task.id}]\n{output}"
        except subprocess.TimeoutExpired:
            return f"⏱ Tests [{task.id}] timed out after 5 min — cache may still be warming, try again in 30s"
        except Exception as e:
            return f"❌ Tests [{task.id}] error: {e}"

    # ── UI check ─────────────────────────────────────────────────────────────
    elif task.type == TaskType.UI_CHECK:
        ref = task.context.get("reference_image")
        if ref:
            ref = bytes(ref)
        path = task.context.get("path", "/")
        analysis = ui_validator.validate_ui(path, task.prompt, ref)
        return f"👁 *UI Check* `[{task.id}]`\n{analysis}"

    # ── Staging deploy ───────────────────────────────────────────────────────
    elif task.type == TaskType.DEPLOY:
        return _handle_deploy(task)

    # ── PR operations ────────────────────────────────────────────────────────
    elif task.type == TaskType.PR:
        return _handle_pr(task)

    return f"⚠️ Unknown task type: {task.type}"


def _handle_deploy(task: Task) -> str:
    """Promote current origin/main HEAD to staging by updating kustomization.yaml."""
    worktree = f"/worktrees/deploy-{task.id}"
    branch = f"fix/staging-promote-{task.id}"
    try:
        coding._ensure_base_repo()
        coding._run(["git", "-C", coding.REPO_PATH, "fetch", "origin"], "fetch")

        sha_full = coding._run(
            ["git", "-C", coding.REPO_PATH, "rev-parse", "origin/main"], "get sha"
        ).strip()
        short_sha = sha_full[:7]

        # Clean up any stale state
        subprocess.run(["git", "-C", coding.REPO_PATH, "worktree", "remove", "--force", worktree], capture_output=True)
        subprocess.run(["git", "-C", coding.REPO_PATH, "branch", "-D", branch], capture_output=True)

        coding._run(
            ["git", "-C", coding.REPO_PATH, "worktree", "add", "-b", branch, worktree, "origin/main"],
            "create worktree",
        )
        subprocess.run(["git", "-C", worktree, "config", "user.email", "devbot@governorai.ai"], capture_output=True)
        subprocess.run(["git", "-C", worktree, "config", "user.name", "DevBot"], capture_output=True)
        subprocess.run(
            ["git", "-C", worktree, "remote", "set-url", "origin",
             f"https://{GITHUB_TOKEN}@github.com/{coding.GITHUB_REPO}.git"],
            capture_output=True,
        )

        # Update image tags in kustomization.yaml
        kpath = os.path.join(worktree, "k8s/overlays/staging/kustomization.yaml")
        with open(kpath) as f:
            content = f.read()

        for image in ("governor-control-plane", "governor-frontend"):
            content = re.sub(
                rf"(name: PLACEHOLDER_ECR/{re.escape(image)}\n\s+newName:[^\n]+\n\s+newTag:)\s+sha-\w+",
                rf"\1 sha-{short_sha}",
                content,
            )

        with open(kpath, "w") as f:
            f.write(content)

        coding._run(["git", "-C", worktree, "add", "k8s/overlays/staging/kustomization.yaml"], "git add")
        coding._run(
            ["git", "-C", worktree, "commit", "-m",
             f"fix(staging): promote sha-{short_sha} to staging\n\nAuto-promotion by devbot."],
            "git commit",
        )
        coding._run(["git", "-C", worktree, "push", "-u", "origin", branch], "git push")

        pr = subprocess.run(
            ["gh", "pr", "create",
             "--title", f"fix(staging): promote sha-{short_sha} to staging",
             "--body", f"Auto-promotion by devbot.\n\nUpdates control-plane and frontend to `sha-{short_sha}`.\n\n⚠️ Images must be pushed to ECR to complete the deploy.",
             "--base", "main"],
            capture_output=True, text=True, cwd=worktree, env=GH_ENV, timeout=30,
        )
        pr_url = pr.stdout.strip().split()[-1] if pr.returncode == 0 else "(PR creation failed)"
        return (
            f"🚀 *Staging promotion ready* `[{task.id}]`\n"
            f"Tag: `sha-{short_sha}`\n"
            f"PR: {pr_url}\n\n"
            f"⚠️ Merge the PR, then push images to ECR to complete the deploy."
        )
    except Exception as e:
        log.error(f"Deploy failed: {e}")
        return f"❌ *Deploy failed* `[{task.id}]`\n{e}"
    finally:
        subprocess.run(["git", "-C", coding.REPO_PATH, "worktree", "remove", "--force", worktree], capture_output=True)


def _handle_pr(task: Task) -> str:
    """Run gh CLI commands based on what the user asked."""
    prompt = task.prompt.lower()

    # Extract PR number if mentioned
    pr_num_match = re.search(r"#?(\d{3,5})", task.prompt)
    pr_num = pr_num_match.group(1) if pr_num_match else None

    try:
        if any(w in prompt for w in ["merge", "land"]) and pr_num:
            r = subprocess.run(
                ["gh", "pr", "merge", pr_num, "--squash", "--auto"],
                capture_output=True, text=True, env=GH_ENV,
                cwd=coding.REPO_PATH, timeout=30,
            )
            out = r.stdout.strip() or r.stderr.strip()
            return f"{'✅' if r.returncode == 0 else '❌'} *Merge PR #{pr_num}* `[{task.id}]`\n{out}"

        elif any(w in prompt for w in ["comment", "review", "feedback"]) and pr_num:
            r = subprocess.run(
                ["gh", "pr", "view", pr_num, "--comments"],
                capture_output=True, text=True, env=GH_ENV,
                cwd=coding.REPO_PATH, timeout=30,
            )
            return f"💬 *PR #{pr_num} comments* `[{task.id}]`\n```\n{r.stdout[-2000:]}\n```"

        elif any(w in prompt for w in ["check", "status", "ci"]) and pr_num:
            r = subprocess.run(
                ["gh", "pr", "checks", pr_num],
                capture_output=True, text=True, env=GH_ENV,
                cwd=coding.REPO_PATH, timeout=30,
            )
            return f"🔍 *PR #{pr_num} checks* `[{task.id}]`\n```\n{r.stdout[-1500:]}\n```"

        elif pr_num:
            r = subprocess.run(
                ["gh", "pr", "view", pr_num],
                capture_output=True, text=True, env=GH_ENV,
                cwd=coding.REPO_PATH, timeout=30,
            )
            return f"📋 *PR #{pr_num}* `[{task.id}]`\n```\n{r.stdout[-2000:]}\n```"

        else:
            # List open PRs
            r = subprocess.run(
                ["gh", "pr", "list", "--state", "open", "--limit", "10"],
                capture_output=True, text=True, env=GH_ENV,
                cwd=coding.REPO_PATH, timeout=30,
            )
            out = r.stdout.strip() or "(no open PRs)"
            return f"📋 *Open PRs* `[{task.id}]`\n```\n{out}\n```"

    except Exception as e:
        return f"❌ *PR operation failed* `[{task.id}]`\n{e}"


def _send(chat_id: int, text: str) -> None:
    import httpx as _httpx
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Try with Markdown first, fall back to plain text on any failure
    r = _httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    if r.status_code != 200:
        log.warning(f"Telegram Markdown send failed ({r.status_code}), retrying as plain text")
        r2 = _httpx.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        if r2.status_code != 200:
            log.error(f"Telegram send failed: {r2.text}")


def main() -> None:
    log.info("Worker running...")
    while True:
        task = q.pop(timeout=5)
        if not task:
            continue

        # Guard: CODE tasks must have a plan from the orchestrator
        if task.type == TaskType.CODE and "plan" not in task.context:
            log.warning(f"Task {task.id} has no plan yet — re-queuing")
            q.push(task)
            continue

        log.info(f"Executing {task.id} type={task.type}")
        _send(task.chat_id, f"⚙️ Working on it...")

        try:
            result_msg = handle(task)
            q.update_session(task.user_id, {"last_result": result_msg[:500]})
            _send(task.chat_id, result_msg)
        except Exception as e:
            log.error(f"Worker failed on {task.id}: {e}")
            _send(task.chat_id, f"❌ Failed: {e}")


if __name__ == "__main__":
    main()
