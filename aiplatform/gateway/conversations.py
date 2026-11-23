"""In-memory conversation store (POC choice, ADR-0004; open item in docs/design-backlog.md).

Keeps, per conversation: the owner, the agent's ConversationContext (last 6 turns plus the
last successful MetricQuery, used for follow-ups) and the message records for history
reload and feedback lookups. Lost on restart and not shared between API replicas.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from assistants.data.agent.agent import ConversationContext

MAX_CONVERSATIONS = 2000
TTL_S = 8 * 3600


@dataclass
class MessageRecord:
    message_id: str
    question: str
    created_at: float
    events: list[dict[str, Any]] = field(default_factory=list)

    def get(self, kind: str) -> dict[str, Any] | None:
        return next((e for e in reversed(self.events) if e["type"] == kind), None)

    def summary(self) -> dict[str, Any]:
        done = self.get("done") or {}
        return {
            "message_id": self.message_id,
            "question": self.question,
            "created_at": self.created_at,
            "route": done.get("route"),
            "status": done.get("status"),
            "events": self.events,
        }


@dataclass
class Conversation:
    id: uuid.UUID
    owner: str
    ctx: ConversationContext
    messages: list[MessageRecord] = field(default_factory=list)
    touched: float = field(default_factory=time.time)


class ConversationStore:
    def __init__(self, max_items: int = MAX_CONVERSATIONS, ttl_s: int = TTL_S) -> None:
        self._items: OrderedDict[uuid.UUID, Conversation] = OrderedDict()
        self._lock = threading.Lock()
        self.max_items, self.ttl_s = max_items, ttl_s

    def _evict(self, now: float) -> None:
        for cid in [c for c, v in self._items.items() if now - v.touched > self.ttl_s]:
            del self._items[cid]
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)

    def get_or_create(self, conversation_id: uuid.UUID | None, owner: str) -> Conversation:
        """A conversation belongs to one user; anyone else gets a fresh one (no leaks)."""
        now = time.time()
        with self._lock:
            self._evict(now)
            conv = self._items.get(conversation_id) if conversation_id else None
            if conv is None or conv.owner != owner:
                cid = uuid.uuid4()
                conv = Conversation(cid, owner, ConversationContext(conversation_id=cid))
                self._items[cid] = conv
            conv.touched = now
            self._items.move_to_end(conv.id)
            return conv

    def get(self, conversation_id: uuid.UUID, owner: str) -> Conversation | None:
        with self._lock:
            conv = self._items.get(conversation_id)
            return conv if conv is not None and conv.owner == owner else None

    def locate(self, message_id: str, owner: str) -> tuple[uuid.UUID, MessageRecord] | None:
        """The owner's message and its conversation id (other users' messages are invisible)."""
        with self._lock:
            for conv in self._items.values():
                if conv.owner != owner:
                    continue
                for m in conv.messages:
                    if m.message_id == message_id:
                        return conv.id, m
        return None

    def find_message(self, message_id: str, owner: str) -> MessageRecord | None:
        found = self.locate(message_id, owner)
        return found[1] if found else None
