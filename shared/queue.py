import json
import redis
from shared.models import Task

ORCH_QUEUE = "devbot:orch"      # bot → orchestrator
WORKER_QUEUE = "devbot:worker"  # orchestrator → worker
SESSION_PREFIX = "devbot:session:"
CANCEL_PREFIX = "devbot:cancel:"
CONV_PREFIX = "devbot:conv:"       # per-user DeepSeek conversation history
CHATMODE_PREFIX = "devbot:chat:"   # per-user chat mode flag
PENDING_PREFIX = "devbot:pending:" # per-user pending task awaiting user input

MAX_CONV_HISTORY = 40   # keep last 40 turns (20 exchanges)
CONV_TTL = 86400        # conversation history expires after 24 hours of inactivity
PENDING_TTL = 3600      # pending task expires after 1 hour if ignored


class Queue:
    def __init__(self, url: str):
        self.r = redis.from_url(url, decode_responses=True)

    # ── Task queues ──────────────────────────────────────────────────────────

    def push(self, task: Task, queue: str = WORKER_QUEUE) -> None:
        self.r.rpush(queue, json.dumps(task.to_dict()))

    def push_orch(self, task: Task) -> None:
        self.push(task, ORCH_QUEUE)

    def pop(self, timeout: int = 5, queue: str = WORKER_QUEUE) -> "Task | None":
        result = self.r.blpop(queue, timeout=timeout)
        if result:
            _, raw = result
            return Task.from_dict(json.loads(raw))
        return None

    def pop_orch(self, timeout: int = 5) -> "Task | None":
        return self.pop(timeout, ORCH_QUEUE)

    # ── Queue inspection ────────────────────────────────────────────────────

    def queue_length(self, queue: str = WORKER_QUEUE) -> int:
        return self.r.llen(queue)

    def orch_length(self) -> int:
        return self.r.llen(ORCH_QUEUE)

    # ── Cancel support ──────────────────────────────────────────────────────

    def set_cancel(self, task_id: str) -> None:
        self.r.setex(f"{CANCEL_PREFIX}{task_id}", 3600, "1")

    def is_cancelled(self, task_id: str) -> bool:
        return bool(self.r.get(f"{CANCEL_PREFIX}{task_id}"))

    def clear_cancel(self, task_id: str) -> None:
        self.r.delete(f"{CANCEL_PREFIX}{task_id}")

    # ── Session context (per Telegram user) ─────────────────────────────────

    def set_session(self, user_id: int, data: dict, ttl: int = 3600) -> None:
        self.r.setex(f"{SESSION_PREFIX}{user_id}", ttl, json.dumps(data))

    def get_session(self, user_id: int) -> dict:
        raw = self.r.get(f"{SESSION_PREFIX}{user_id}")
        return json.loads(raw) if raw else {}

    def update_session(self, user_id: int, updates: dict) -> None:
        session = self.get_session(user_id)
        session.update(updates)
        self.set_session(user_id, session)

    # ── Chat mode (persistent DeepSeek conversation) ─────────────────────────

    def is_chat_mode(self, user_id: int) -> bool:
        return bool(self.r.get(f"{CHATMODE_PREFIX}{user_id}"))

    def set_chat_mode(self, user_id: int, enabled: bool) -> None:
        if enabled:
            self.r.set(f"{CHATMODE_PREFIX}{user_id}", "1")
        else:
            self.r.delete(f"{CHATMODE_PREFIX}{user_id}")

    # ── Conversation history ─────────────────────────────────────────────────

    def get_conversation(self, user_id: int) -> list[dict]:
        raw = self.r.get(f"{CONV_PREFIX}{user_id}")
        return json.loads(raw) if raw else []

    def append_conversation(self, user_id: int, role: str, content: str) -> None:
        history = self.get_conversation(user_id)
        history.append({"role": role, "content": content})
        if len(history) > MAX_CONV_HISTORY:
            history = history[-MAX_CONV_HISTORY:]
        self.r.setex(f"{CONV_PREFIX}{user_id}", CONV_TTL, json.dumps(history))

    def clear_conversation(self, user_id: int) -> None:
        self.r.delete(f"{CONV_PREFIX}{user_id}")

    # ── Pending task (plan approval / clarification / retry) ─────────────────

    def get_pending(self, user_id: int) -> "dict | None":
        raw = self.r.get(f"{PENDING_PREFIX}{user_id}")
        return json.loads(raw) if raw else None

    def set_pending(self, user_id: int, state: str, task: "Task", **extra) -> None:
        """Store a task that is waiting for user input before proceeding."""
        payload = {"state": state, "task": task.to_dict(), **extra}
        self.r.setex(f"{PENDING_PREFIX}{user_id}", PENDING_TTL, json.dumps(payload))

    def clear_pending(self, user_id: int) -> None:
        self.r.delete(f"{PENDING_PREFIX}{user_id}")
