"""
Telegram bot — the I/O layer.

Accepts messages/photos from allowed users, pushes to the orchestrator queue,
and sends results back. Nothing is computed here.
"""

import asyncio
import logging
import os
import sys

sys.path.insert(0, "/app")

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from shared.models import Task, TaskType
from shared.queue import Queue

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

ALLOWED_IDS = set(int(x) for x in os.environ["ALLOWED_TELEGRAM_IDS"].split(","))
q = Queue(os.environ["REDIS_URL"])


def allowed(update: Update) -> bool:
    return update.effective_user.id in ALLOWED_IDS


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_text(
        "👋 DevBot ready.\n\n"
        "▸ Describe a feature/fix to implement\n"
        "▸ 'run tests [path]'\n"
        "▸ 'check staging UI [path]' (attach a screenshot to compare)\n"
        "▸ 'deploy to staging'\n"
        "▸ 'list PRs', 'merge PR #N', 'comments on PR #N'\n"
        "▸ Any question about the repo — I'll check GitHub\n"
        "\n"
        "Classification hints:\n"
        "  • Questions ending with ? → STATUS (GitHub data)\n"
        "  • Action verbs (add, fix, implement) → CODE\n"
        "  • 'merge PR', 'list PRs', 'PR #' → PR ops\n"
        "  • 'deploy to staging', 'ship' → DEPLOY\n"
        "\n"
        "Commands:\n"
        "  /chat              — start a direct conversation with DeepSeek\n"
        "  /done              — exit chat mode and clear history\n"
        "  /cancel <task_id>  — abort a running task\n"
        "  /queue             — see what's pending\n"
    )


async def chat_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Enter persistent chat mode — all messages go directly to DeepSeek."""
    if not allowed(update):
        return
    user_id = update.effective_user.id
    q.set_chat_mode(user_id, True)
    q.clear_conversation(user_id)
    await update.message.reply_text(
        "💬 *Chat mode on.* Talking directly to DeepSeek.\n\n"
        "Your conversation history is kept across messages so it can follow along. "
        "Send /done to exit and clear the history.",
        parse_mode="Markdown",
    )


async def chat_end(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Exit chat mode and wipe conversation history."""
    if not allowed(update):
        return
    user_id = update.effective_user.id
    q.set_chat_mode(user_id, False)
    q.clear_conversation(user_id)
    await update.message.reply_text("✅ Back to bot mode. Conversation cleared.")


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a running task by ID."""
    if not allowed(update):
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /cancel <task_id>")
        return
    task_id = args[0]
    q.set_cancel(task_id)
    await update.message.reply_text(f"🚫 Cancel requested for `[{task_id}]`.", parse_mode="Markdown")


async def queue_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show pending task queue lengths."""
    if not allowed(update):
        return
    orch_len = q.orch_length()
    worker_len = q.queue_length()
    await update.message.reply_text(
        f"📊 Queue status:\n"
        f"  Orchestrator (planning): {orch_len} pending\n"
        f"  Worker (execution):      {worker_len} pending"
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    if q.is_chat_mode(user_id):
        # In chat mode: route directly to DeepSeek, no task ID noise
        task = Task(
            user_id=user_id,
            chat_id=chat_id,
            prompt=text,
            type=TaskType.CHAT,
        )
        q.push_orch(task)
        await ctx.bot.send_chat_action(chat_id=chat_id, action="typing")
        return

    task = Task(
        user_id=user_id,
        chat_id=chat_id,
        prompt=text,
        type=_classify(text),
    )

    q.push_orch(task)
    await update.message.reply_text(f"⏳ Got it `[{task.id}]` — on it.", parse_mode="Markdown")


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    caption = (update.message.caption or "check staging ui").strip()

    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    image_bytes = await file.download_as_bytearray()

    task = Task(
        user_id=user_id,
        chat_id=chat_id,
        prompt=caption,
        type=TaskType.UI_CHECK,
        context={"reference_image": list(image_bytes)},
    )

    q.push_orch(task)
    await update.message.reply_text(
        f"📸 Got your screenshot `[{task.id}]` — comparing against staging.",
        parse_mode="Markdown",
    )


def _classify(text: str) -> TaskType:
    t = text.lower().strip()

    # ── Explicit / specific phrases (highest priority) ──────────────────────
    # Check BEFORE generic action verbs to avoid misclassifying compound queries

    if any(w in t for w in ["deploy to staging", "push to staging", "promote to staging",
                              "promote staging", "ship to staging"]):
        return TaskType.DEPLOY
    if any(w in t for w in ["merge pr", "merge pull", "list pr", "list pull",
                              "open pr", "create pr", "pull request", "check pr",
                              "comments on pr", "pr status", "pr #", "approve pr",
                              "pr 1", "pr 2", "pr 3", "pr 4", "pr 5",
                              "pr 6", "pr 7", "pr 8", "pr 9", "pr 0"]):
        return TaskType.PR
    if any(w in t for w in ["check staging ui", "screenshot", "ui check",
                              "visual check", "compare staging", "looks like figma"]):
        return TaskType.UI_CHECK
    if any(w in t for w in ["run tests", "run test", "test ", "ci status", "playwright"]):
        return TaskType.TEST

    # ── Compound queries: pick the first action verb that appears ───────────
    # If the message contains multiple intents ("before we merge can you deploy"),
    # pick the first action word as the primary intent.
    action_verbs = [
        ("deploy", TaskType.DEPLOY),
        ("merge", TaskType.PR),
        ("create pr", TaskType.PR),
        ("list pr", TaskType.PR),
        ("add", TaskType.CODE),
        ("create", TaskType.CODE),
        ("fix", TaskType.CODE),
        ("update", TaskType.CODE),
        ("implement", TaskType.CODE),
        ("write", TaskType.CODE),
        ("change", TaskType.CODE),
        ("remove", TaskType.CODE),
        ("delete", TaskType.CODE),
        ("refactor", TaskType.CODE),
        ("build", TaskType.CODE),
        ("make", TaskType.CODE),
        ("edit", TaskType.CODE),
        ("rename", TaskType.CODE),
        ("move", TaskType.CODE),
        ("replace", TaskType.CODE),
        ("migrate", TaskType.CODE),
        ("upgrade", TaskType.CODE),
        ("install", TaskType.CODE),
        ("configure", TaskType.CODE),
        ("wire", TaskType.CODE),
    ]
    first_pos = len(t) + 1
    best_type = TaskType.CODE  # fallback
    for word, task_type in action_verbs:
        pos = t.find(word)
        if pos != -1 and pos < first_pos:
            first_pos = pos
            best_type = task_type
    if first_pos < len(t):
        return best_type

    # ── Remaining loose keywords ────────────────────────────────────────────
    if any(w in t for w in ["deploy", "ship", "release"]):
        return TaskType.DEPLOY
    if any(w in t for w in ["merge", "pr", "pull request"]):
        return TaskType.PR
    if any(w in t for w in ["test", "ci"]):
        return TaskType.TEST
    if any(w in t for w in ["ui", "visual"]):
        return TaskType.UI_CHECK

    # ── Questions → STATUS (orchestrator answers with real GitHub context) ──
    if t.endswith("?") or any(t.startswith(w) for w in [
        "what", "does", "how", "why", "who", "when", "is ", "are ", "explain",
        "tell me", "describe", "show me", "can ", "status",
        "what's", "whats", "which",
    ]):
        return TaskType.STATUS

    return TaskType.CODE


def main() -> None:
    app = (
        Application.builder()
        .token(os.environ["TELEGRAM_BOT_TOKEN"])
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chat", chat_start))
    app.add_handler(CommandHandler("done", chat_end))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("queue", queue_status))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
