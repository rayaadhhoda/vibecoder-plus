import json
import redis
from shared.models import Task

ORCH_QUEUE = "devbot:orch"      # bot → orchestrator
WORKER_QUEUE = "devbot:worker"  # orchestrator → worker
SESSION_PREFIX = "devbot:session:"


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
