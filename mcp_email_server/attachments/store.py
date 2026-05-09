from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def _default_ttl_minutes() -> int:
    try:
        return int(os.environ.get("MCP_EMAIL_ATTACHMENT_TTL_MINUTES", "30"))
    except ValueError:
        return 30


@dataclass
class _Entry:
    data: bytes
    filename: str
    mime_type: str
    expires_at: datetime


@dataclass
class AttachmentStore:
    """In-memory store for temporarily serving email attachments via URL.

    Each stored attachment gets an unguessable UUID token. Entries expire
    after `ttl_minutes` and are cleaned up by a background task started
    via `start_cleanup_task()`.
    """

    ttl_minutes: int = field(default_factory=_default_ttl_minutes)
    _entries: dict[str, _Entry] = field(default_factory=dict, init=False, repr=False)
    _cleanup_task: asyncio.Task | None = field(default=None, init=False, repr=False)

    def put(self, data: bytes, filename: str, mime_type: str) -> str:
        """Store attachment bytes and return an opaque token."""
        token = uuid.uuid4().hex
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=self.ttl_minutes)
        self._entries[token] = _Entry(
            data=data,
            filename=filename,
            mime_type=mime_type,
            expires_at=expires_at,
        )
        return token

    def get(self, token: str) -> _Entry | None:
        """Retrieve an entry by token, or None if missing/expired."""
        entry = self._entries.get(token)
        if entry is None:
            return None
        if datetime.now(timezone.utc) >= entry.expires_at:
            del self._entries[token]
            return None
        return entry

    def _purge_expired(self) -> None:
        now = datetime.now(timezone.utc)
        expired = [t for t, e in self._entries.items() if now >= e.expires_at]
        for token in expired:
            del self._entries[token]

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            self._purge_expired()

    def start_cleanup_task(self) -> None:
        """Start the background expiry cleanup task. Call once at server startup."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def stop_cleanup_task(self) -> None:
        """Cancel the background cleanup task. Call at server shutdown."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            self._cleanup_task = None


# Module-level singleton used by the MCP tools and HTTP handler.
attachment_store = AttachmentStore()
