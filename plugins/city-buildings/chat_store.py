"""chat_store — persistent AI conversations, per user, one JSON file per chat.

Lives in SOTRICE_AI_DIR/chats (the same per-user dir the AI keys / usage use, outside any plugin install dir),
so chats survive closing the AI window and restarting the app. A chat is {id, title, created, updated, turns};
a turn is one question + its answer: {id, q, a, status, usage?, voice?, ts}.

`recent_context` is what keeps a follow-up cheap: only the last few turns, each clipped, ever go back to the model
("new conversation" = a new chat id = an empty context).
"""

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

MAX_CHATS = 200
MAX_TURNS_PER_CHAT = 300
CONTEXT_TURNS = 6
CONTEXT_Q_CHARS = 240
CONTEXT_A_CHARS = 420
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{5,47}$")


def ai_dir() -> Path:
    override = os.environ.get("SOTRICE_AI_DIR")
    return Path(override) if override else Path(os.environ.get("APPDATA") or Path.home()) / "Sotrice" / "ai"


def valid_id(chat_id: Any) -> bool:
    return isinstance(chat_id, str) and bool(_ID.match(chat_id))


class ChatStore:
    def __init__(self, root: Optional[Path] = None, scope: str = "city"):
        # `scope` keeps one agent's chats apart from another's (the city agent today, other spins later).
        self.root = (Path(root) if root else ai_dir() / "chats") / scope
        self._lock = threading.RLock()

    def _path(self, chat_id: str) -> Path:
        if not valid_id(chat_id):
            raise ValueError("bad chat id")
        return self.root / f"{chat_id}.json"

    def _write(self, chat: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(chat["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def new_id(self) -> str:
        return "c" + uuid.uuid4().hex[:12]

    def create(self) -> dict:
        now = time.time()
        chat = {"id": self.new_id(), "title": "", "created": now, "updated": now, "turns": []}
        with self._lock:
            self._write(chat)
            self._prune()
        return chat

    def load(self, chat_id: str) -> Optional[dict]:
        try:
            with self._lock:
                return json.loads(self._path(chat_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def list_chats(self) -> "list[dict]":
        out = []
        with self._lock:
            if not self.root.exists():
                return []
            for p in self.root.glob("*.json"):
                try:
                    c = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if valid_id(c.get("id")) and c["turns"]:
                    out.append({"id": c["id"], "title": c.get("title") or "", "updated": c.get("updated", 0), "turns": len(c["turns"])})
        out.sort(key=lambda c: -c["updated"])
        return out

    def append(self, chat_id: str, turn: dict) -> Optional[dict]:
        """Adds a finished turn (creating the chat file if the id is new). Returns the stored chat summary."""
        with self._lock:
            chat = self.load(chat_id) or {"id": chat_id, "title": "", "created": time.time(), "updated": 0, "turns": []}
            q = str(turn.get("q") or "")
            chat["turns"].append({
                "id": str(turn.get("id") or uuid.uuid4().hex[:8]), "q": q[:2000], "a": str(turn.get("a") or "")[:4000],
                "status": turn.get("status") or "ok", "usage": turn.get("usage"), "voice": bool(turn.get("voice")), "ts": time.time(),
            })
            del chat["turns"][:-MAX_TURNS_PER_CHAT]
            if not chat["title"] and q.strip():
                chat["title"] = q.strip().replace("\n", " ")[:60]
            chat["updated"] = time.time()
            self._write(chat)
            self._prune()
            return {"id": chat["id"], "title": chat["title"], "updated": chat["updated"], "turns": len(chat["turns"])}

    def amend(self, chat_id: str, turn_id: str, answer: str, status: str) -> None:
        with self._lock:
            chat = self.load(chat_id)
            if not chat:
                return
            for t in chat["turns"]:
                if t.get("id") == turn_id:
                    t["a"], t["status"] = answer[:4000], status
                    self._write(chat)
                    return

    def delete(self, chat_id: str) -> bool:
        try:
            with self._lock:
                self._path(chat_id).unlink()
            return True
        except (OSError, ValueError):
            return False

    def recent_context(self, chat_id: Optional[str]) -> list[dict]:
        """The last few turns, clipped, for the model. Empty for a fresh/unknown chat."""
        chat = self.load(chat_id) if chat_id else None
        if not chat:
            return []
        out = []
        for t in chat["turns"][-CONTEXT_TURNS:]:
            if t.get("status") == "error" or not t.get("a"):
                continue
            out.append({"q": (t["q"] or "(voice message)")[:CONTEXT_Q_CHARS], "a": t["a"][:CONTEXT_A_CHARS]})
        return out

    def _prune(self) -> None:
        files = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[MAX_CHATS:]:
            try:
                p.unlink()
            except OSError:
                pass
