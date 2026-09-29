from PySide6.QtCore import Qt, QTimer
from collections import deque
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from src.models import ChannelOut, TaskOut
from src.utils import format_local, task_sort_key
from src.widgets.channel_labels import dashboard_tag_text, authorization_text, age_color, upload_color

AVATAR_SIZE = 64


class ChannelCard(QFrame):
    def __init__(self, channel: ChannelOut, parent=None) -> None:
        super().__init__(parent)
        self.channel = channel
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumWidth(220)
        self.setMaximumWidth(320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        self.avatar_label = QLabel()
        self.avatar_label.setFixedSize(AVATAR_SIZE, AVATAR_SIZE)
        self.avatar_label.setAlignment(Qt.AlignCenter)
        self.avatar_label.setStyleSheet(
            "background-color: #e0e0e0; border-radius: 8px;"
        )
        layout.addWidget(self.avatar_label, alignment=Qt.AlignLeft)

        title = QLabel(f"<b>{channel.youtube_title or channel.title or 'Untitled'}</b>")
        title.setWordWrap(True)
        layout.addWidget(title)
        self.title_label = title
        self.title_label.setTextFormat(Qt.PlainText)
        self.title_label.setText(channel.youtube_title or channel.title or "Untitled")

        handle = QLabel(f"<span style='color:#666'>{channel.handle or ''}</span>")
        layout.addWidget(handle)
        self.handle_label = handle
        self.handle_label.setTextFormat(Qt.PlainText)
        self.handle_label.setText(channel.handle or "")
        self.tags_label = QLabel(dashboard_tag_text(channel))
        self.tags_label.setTextFormat(Qt.PlainText)
        self.tags_label.setWordWrap(True)
        self.tags_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        layout.addWidget(self.tags_label)
        self.authorized_label = QLabel()
        self.authorized_label.setWordWrap(True)
        self.authorized_label.setToolTip("First authorization on this server. Blue: <7d; purple: 7–29d; teal: 30+d.")
        layout.addWidget(self.authorized_label)
        self.uploaded_label = QLabel("Uploaded: —")
        self.uploaded_label.setToolTip("Server upload count. Gray: 0; blue: 1–99; teal: 100+. —: unavailable or loading.")
        layout.addWidget(self.uploaded_label)
        self._refresh_labels(channel)

        status_text = f"Status: {channel.status}"
        session_text = f"Session: {channel.session_status}"
        self.status_label = QLabel(status_text)
        self.session_label = QLabel(session_text)
        layout.addWidget(self.status_label)
        layout.addWidget(self.session_label)

        self.pause_label = QLabel("")
        layout.addWidget(self.pause_label)
        if channel.paused_until:
            self.pause_label.setText(f"Paused until: {format_local(channel.paused_until, '%H:%M')}")
        else:
            self.pause_label.hide()

        self.pause_reason_label = QLabel("")
        layout.addWidget(self.pause_reason_label)
        if channel.pause_reason:
            self.pause_reason_label.setText(f"Pause reason: {channel.pause_reason}")
        else:
            self.pause_reason_label.hide()

        layout.addItem(QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding))

    def set_avatar(self, pixmap: QPixmap) -> None:
        scaled = pixmap.scaled(
            AVATAR_SIZE,
            AVATAR_SIZE,
            Qt.KeepAspectRatioByExpanding,
            Qt.SmoothTransformation,
        )
        self.avatar_label.setPixmap(scaled)

    def update_channel(self, channel: ChannelOut) -> None:
        if self.channel == channel:
            return
        self.channel = channel
        self.title_label.setText(channel.youtube_title or channel.title or "Untitled")
        self.handle_label.setText(channel.handle or "")
        self._refresh_labels(channel)
        self.status_label.setText(f"Status: {channel.status}")
        self.session_label.setText(f"Session: {channel.session_status}")
        if channel.paused_until:
            self.pause_label.setText(f"Paused until: {format_local(channel.paused_until, '%H:%M')}")
            self.pause_label.show()
        else:
            self.pause_label.hide()
        if channel.pause_reason:
            self.pause_reason_label.setText(f"Pause reason: {channel.pause_reason}")
            self.pause_reason_label.show()
        else:
            self.pause_reason_label.hide()


    def _refresh_labels(self, channel):
        self.tags_label.setText(dashboard_tag_text(channel))
        self.authorized_label.setText(f"Authorized: {authorization_text(channel)}")
        style = f"background: {age_color(channel)}; color: #263238; padding: 4px; border-radius: 4px;"
        if self.authorized_label.styleSheet() != style:
            self.authorized_label.setStyleSheet(style)

    def set_uploaded_count(self, count):
        if getattr(self, "_count", object()) == count:
            return
        self._count = count
        self.uploaded_label.setText(f"Uploaded: {count if count is not None else '—'}")
        self.uploaded_label.setStyleSheet(f"background: {upload_color(count)}; color: #263238; padding: 4px; border-radius: 4px;")


class DashboardWidget(QScrollArea):
    MAX_ACTIVE_UPLOADS = 50

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._container = QWidget()
        self.setWidget(self._container)
        self._layout = QVBoxLayout(self._container)
        self._layout.setAlignment(Qt.AlignTop)

        # Channel cards grid
        self._channels_label = QLabel("<h3>Channels</h3>")
        self._layout.addWidget(self._channels_label)

        self._cards_grid = QGridLayout()
        self._cards_grid.setSpacing(10)
        self._layout.addLayout(self._cards_grid)
        self._cards: dict[str, ChannelCard] = {}
        self._uploaded_counts = {}
        self._avatars = {}
        self._channel_by_id = {}
        self._card_queue = deque()
        self._card_timer = QTimer(self)
        self._card_timer.setSingleShot(True)
        self._card_timer.timeout.connect(self._render_cards)

        # Active uploads
        self._uploads_label = QLabel("<h3>Active Uploads</h3>")
        self._layout.addWidget(self._uploads_label)
        self._uploads_layout = QVBoxLayout()
        self._uploads_layout.setAlignment(Qt.AlignTop)
        self._layout.addLayout(self._uploads_layout)
        self._upload_bars: dict[str, tuple[QLabel, QProgressBar]] = {}

        # Event log
        self._events_label = QLabel("<h3>Recent Events</h3>")
        self._layout.addWidget(self._events_label)
        self._events_layout = QVBoxLayout()
        self._events_layout.setAlignment(Qt.AlignTop)
        self._layout.addLayout(self._events_layout)
        self._events: list[QLabel] = []
        self._channels: list[ChannelOut] = []

        self._layout.addItem(QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding))

    def set_channels(self, channels: list[ChannelOut]) -> None:
        self._channels = list(channels)
        self._channel_by_id = {str(c.id): c for c in channels if c.status != "deleted"}
        for sid in self._cards.keys() - self._channel_by_id.keys():
            card = self._cards.pop(sid)
            self._cards_grid.removeWidget(card)
            card.deleteLater()
        self._card_queue = deque(enumerate(self._channel_by_id))
        self._render_cards()

    def _render_cards(self):
        for _ in range(min(12, len(self._card_queue))):
            position, sid = self._card_queue.popleft()
            ch = self._channel_by_id[sid]
            card = self._cards.get(sid)
            if card is None:
                card = ChannelCard(ch)
                self._cards[sid] = card
                card.set_uploaded_count(self._uploaded_counts.get(sid))
                if sid in self._avatars:
                    card.set_avatar(self._avatars[sid])
            else:
                card.update_channel(ch)
            target = (position // 4, position % 4)
            current = self._cards_grid.indexOf(card)
            if current < 0 or self._cards_grid.getItemPosition(current)[:2] != target:
                self._cards_grid.addWidget(card, *target)
        if self._card_queue:
            self._card_timer.start(1)

    def update_channel(self, channel: ChannelOut) -> None:
        sid = str(channel.id)
        if sid in self._channel_by_id and channel.status != "deleted":
            self._channel_by_id[sid] = channel
            self._channels = [channel if c.id == channel.id else c for c in self._channels]
            if sid in self._cards:
                self._cards[sid].update_channel(channel)
            return
        channels = [item for item in self._channels if item.id != channel.id]
        if channel.status != "deleted":
            channels.append(channel)
        self.set_channels(channels)

    def set_uploaded_count(self, channel_id, count):
        self._uploaded_counts[str(channel_id)] = count
        card = self._cards.get(str(channel_id))
        if card:
            card.set_uploaded_count(count)

    def set_channel_avatar(self, channel_id: str, pixmap: QPixmap) -> None:
        self._avatars[channel_id] = pixmap
        card = self._cards.get(channel_id)
        if card:
            card.set_avatar(pixmap)

    def set_active_uploads(self, tasks: list[TaskOut]) -> None:
        # remove old
        for tid, (label, bar) in list(self._upload_bars.items()):
            label.deleteLater()
            bar.deleteLater()
            del self._upload_bars[tid]
        active = [t for t in tasks if t.status in ("pending", "session_created", "uploading", "retry")]
        active.sort(key=task_sort_key, reverse=True)
        for t in active[: self.MAX_ACTIVE_UPLOADS]:
            self._add_task_widget(t)

    def _add_task_widget(self, task: TaskOut) -> None:
        name = task.file_path.split("/")[-1].split("\\")[-1]
        label = QLabel(f"<b>{name}</b> — {task.status}")
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(task.progress_percent)
        self._uploads_layout.addWidget(label)
        self._uploads_layout.addWidget(bar)
        self._upload_bars[str(task.id)] = (label, bar)

    def update_task(self, task: TaskOut) -> None:
        tid = str(task.id)
        if tid in self._upload_bars:
            label, bar = self._upload_bars[tid]
            name = task.file_path.split("/")[-1].split("\\")[-1]
            label.setText(f"<b>{name}</b> — {task.status}")
            bar.setValue(task.progress_percent)
            if task.status not in ("pending", "session_created", "uploading", "retry"):
                self._remove_task_widget(tid)
        elif task.status in ("pending", "session_created", "uploading", "retry"):
            # New active task — add it
            self._add_task_widget(task)
            self._enforce_upload_limit()

    def update_tasks(self, tasks: list[TaskOut]) -> None:
        active_ids: set[str] = set()
        for task in tasks:
            tid = str(task.id)
            if task.status in ("pending", "session_created", "uploading", "retry"):
                active_ids.add(tid)
            if tid in self._upload_bars:
                label, bar = self._upload_bars[tid]
                name = task.file_path.split("/")[-1].split("\\")[-1]
                label.setText(f"<b>{name}</b> — {task.status}")
                bar.setValue(task.progress_percent)
            elif task.status in ("pending", "session_created", "uploading", "retry"):
                self._add_task_widget(task)
        for tid in list(self._upload_bars.keys()):
            if tid not in active_ids:
                self._remove_task_widget(tid)
        self._enforce_upload_limit()

    def _remove_task_widget(self, task_id: str) -> None:
        widgets = self._upload_bars.pop(task_id, None)
        if widgets:
            label, bar = widgets
            label.deleteLater()
            bar.deleteLater()

    def _enforce_upload_limit(self) -> None:
        if len(self._upload_bars) <= self.MAX_ACTIVE_UPLOADS:
            return
        sorted_items = sorted(
            self._upload_bars.items(),
            key=lambda item: item[1][0].text(),
        )
        # Keep the newest tasks (highest updated_at). We don't store timestamps on
        # widgets, so fall back to removing the oldest added ones (first in dict).
        to_remove = list(self._upload_bars.keys())[: len(self._upload_bars) - self.MAX_ACTIVE_UPLOADS]
        for tid in to_remove:
            self._remove_task_widget(tid)

    def add_event(self, event_text: str, event_type: str = "info") -> None:
        color = "#2e7d32" if event_type == "success" else "#d32f2f" if event_type == "error" else "#ed6c02" if event_type == "warning" else "#333"
        label = QLabel(f"<span style='color:{color}'>•</span> {event_text}")
        label.setWordWrap(True)
        self._events_layout.insertWidget(0, label)
        self._events.insert(0, label)
        if len(self._events) > 30:
            old = self._events.pop()
            old.deleteLater()
