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
        "What to say:\n"
        "  • Describe a feature/fix to implement\n"
        "  • 'run tests [path]'\n"
        "  • 'check staging UI [path]' (attach a screenshot to compare)\n"
        "  • 'deploy to staging'\n"
        "  • 'list PRs', 'merge PR #N', 'comments on PR #N'\n"
        "  • Any question about the repo — I'll check GitHub"
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

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

    # Explicit deploy/PR phrases first (before generic action verbs)
    if any(w in t for w in ["deploy to staging", "push to staging", "promote to staging",
                              "promote staging", "ship to staging"]):
        return TaskType.DEPLOY
    if any(w in t for w in ["merge pr", "merge pull", "list pr", "list pull",
                              "open pr", "create pr", "pull request", "check pr",
                              "comments on pr", "pr status", "pr #", "approve pr"]):
        return TaskType.PR

    # Action verbs → CODE
    if any(t.startswith(w) for w in [
        "add", "create", "fix", "update", "implement", "write", "change",
        "remove", "delete", "refactor", "build", "make", "edit", "rename",
        "move", "replace", "migrate", "upgrade", "install", "configure", "wire",
    ]):
        return TaskType.CODE

    if any(w in t for w in ["test", "run tests", "ci", "playwright"]):
        return TaskType.TEST
    if any(w in t for w in ["ui check", "check staging", "screenshot", "visual", "looks like"]):
        return TaskType.UI_CHECK
    if any(w in t for w in ["deploy", "ship", "release"]):
        return TaskType.DEPLOY

    # Questions → STATUS (orchestrator answers with real GitHub context)
    if t.endswith("?") or any(t.startswith(w) for w in [
        "what", "does", "how", "why", "who", "when", "is ", "are ", "explain",
        "tell me", "describe", "show me", "list", "can ", "status",
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
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
