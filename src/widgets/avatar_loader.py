from __future__ import annotations

from uuid import UUID

import httpx
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap

from src.api_client import ApiClient
from src.api_worker import run_api


class AvatarLoader(QObject):
    """Loads channel avatars asynchronously and caches results."""

    avatar_loaded = Signal(object, object)  # UUID, QPixmap
    avatar_failed = Signal(object, str)  # UUID, message

    def __init__(self, api: ApiClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._api = api
        self._cache: dict[str, QPixmap] = {}
        self._pending: set[str] = set()

    def get_cached(self, channel_id: UUID) -> QPixmap | None:
        return self._cache.get(str(channel_id))

    def load(self, channel_id: UUID) -> None:
        sid = str(channel_id)
        if sid in self._cache or sid in self._pending:
            return
        self._pending.add(sid)

        def on_error(message: str) -> None:
            print(f"[AvatarLoader] failed to load {sid}: {message}")
            self._pending.discard(sid)
            self.avatar_failed.emit(channel_id, message)

        run_api(
            self._api,
            self._fetch,
            self._on_loaded,
            on_error,
            channel_id,
            parent=self,
            background=True,
        )

    def _fetch(self, channel_id: UUID) -> tuple[UUID, bytes | None]:
        avatar = self._api.get_channel_avatar(channel_id)
        url = avatar.thumbnail_url
        if not url:
            return channel_id, None
        resp = httpx.get(
            url,
            timeout=10.0,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": "https://www.youtube.com/",
            },
        )
        resp.raise_for_status()
        return channel_id, resp.content

    def _on_loaded(self, result: tuple[UUID, bytes | None]) -> None:
        channel_id, data = result
        sid = str(channel_id)
        self._pending.discard(sid)
        if not data:
            print(f"[AvatarLoader] no avatar URL for {sid}")
            self.avatar_failed.emit(channel_id, "No avatar URL")
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            print(f"[AvatarLoader] failed to decode avatar for {sid}")
            self.avatar_failed.emit(channel_id, "Failed to decode avatar image")
            return
        print(f"[AvatarLoader] loaded avatar for {sid} size={pixmap.size()}")
        self._cache[sid] = pixmap
        self.avatar_loaded.emit(channel_id, pixmap)
