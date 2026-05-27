from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time
import uuid


class TaskType(str, Enum):
    CODE = "code"          # implement a feature / fix a bug
    TEST = "test"          # run the test suite
    UI_CHECK = "ui_check"  # visual UI validation via Gemini
    DEPLOY = "deploy"      # trigger a deployment
    PR = "pr"              # open / merge / review a PR
    STATUS = "status"      # report current state (one-shot GitHub lookup)
    CHAT = "chat"          # persistent conversational session with DeepSeek


class TaskStatus(str, Enum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    type: TaskType = TaskType.CODE
    status: TaskStatus = TaskStatus.QUEUED
    user_id: int = 0           # Telegram user ID who requested it
    chat_id: int = 0           # Telegram chat to reply to
    prompt: str = ""           # raw user message
    context: dict = field(default_factory=dict)   # extra payload (image bytes, branch name, etc.)
    result: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type.value,
            "status": self.status.value,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "prompt": self.prompt,
            "context": self.context,
            "result": self.result,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d["type"] = TaskType(d["type"])
        d["status"] = TaskStatus(d["status"])
        return cls(**d)
