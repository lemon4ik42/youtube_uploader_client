from datetime import timezone

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QSortFilterProxyModel, QTimer
from PySide6.QtGui import QColor

from src.models import ChannelOut, ProxyOut
from src.widgets.channel_labels import tag_text, authorization_text, authorization_age, age_color, upload_color


class ChannelProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(Qt.UserRole + 1)
        self._query = ""

    def setFilterFixedString(self, text):
        self._query = text.casefold()
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):
        if not self._query:
            return True
        model = self.sourceModel()
        return self._query in model._search[model._channels[row].id] or self._query in model.groups[row].casefold()

    def lessThan(self, left, right):
        model = self.sourceModel()
        if model.grouped:
            a, b = model.groups[left.row()], model.groups[right.row()]
            if a != b:
                return a < b if self.sortOrder() == Qt.AscendingOrder else a > b
        col = left.column()
        a_id, b_id = model._channels[left.row()].id, model._channels[right.row()].id
        a, b = model._sort_keys[a_id][col], model._sort_keys[b_id][col]
        if col == 8:
            a, b = model.groups[left.row()], model.groups[right.row()]
        if left.column() == 7 and (a == -1) != (b == -1):
            return a != -1 if self.sortOrder() == Qt.AscendingOrder else a == -1
        if a == b:
            return a_id.int < b_id.int
        return a < b


class TagGroupProxyModel(ChannelProxyModel):
    """Filtered view of the regular channel model for one tag group."""

    def __init__(self, channel_ids, parent=None):
        super().__init__(parent)
        self._channel_ids = set(channel_ids)

    def filterAcceptsRow(self, row, parent):
        model = self.sourceModel()
        channel = model.get_channel(row)
        return channel is not None and channel.id in self._channel_ids and super().filterAcceptsRow(row, parent)


class ChannelTableModel(QAbstractTableModel):
    COLUMNS = [
        ("Title", lambda c: c.youtube_title or c.title or "Untitled"),
        ("Handle", lambda c: c.handle or ""),
        ("Status", lambda c: c.status),
        ("Session", lambda c: c.session_status),
        ("Auto Upload", lambda c: "On" if c.auto_upload_enabled else "Off"),
        ("Tags", tag_text),
        ("Authorized", lambda c: f"{authorization_age(c)}d"),
        ("Uploaded", None),
        ("Tag group", None),
        ("Proxy", None),
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._channels: list[ChannelOut] = []
        self._proxies: dict[object, ProxyOut] = {}
        self._source_channels = []
        self.grouped = False
        self.groups = []
        self.counts = {}
        self._rows = {}
        self._source_index = {}
        self._display = {}
        self._sort_keys = {}
        self._search = {}
        self._age_timer = QTimer(self)
        self._age_timer.timeout.connect(self._refresh_ages)
        self._age_timer.start(60_000)

    def _refresh_ages(self):
        changed = False
        for channel in self._source_channels:
            if self._display[channel.id][6] != f"{authorization_age(channel)}d":
                self._cache_channel(channel)
                changed = True
        if changed and self._channels:
            self.dataChanged.emit(self.index(0, 6), self.index(len(self._channels) - 1, 6))

    def _cache_channel(self, channel):
        cid = channel.id
        values = [getter(channel) if getter else "" for _, getter in self.COLUMNS]
        count = self.counts.get(str(cid))
        values[7] = "—" if count is None else str(count)
        values[9] = self._proxy_status(channel)
        self._display[cid] = values
        keys = [str(value).casefold() for value in values]
        created = channel.created_at
        keys[6] = (created if created.tzinfo else created.replace(tzinfo=timezone.utc)).timestamp()
        keys[7] = -1 if count is None else count
        self._sort_keys[cid] = keys
        self._search[cid] = "\n".join(str(value).casefold() for value in values)

    def _index_rows(self):
        self._rows = {}
        for row, channel in enumerate(self._channels):
            self._rows.setdefault(channel.id, []).append(row)
        self._source_index = {c.id: i for i, c in enumerate(self._source_channels)}

    def rows_for_channel(self, channel_id):
        return self._rows.get(channel_id, ())

    def needs_regroup(self, channel):
        rows = self._rows.get(channel.id, ())
        return self.grouped and (not rows or sorted({t.name for t in channel.tags}) != sorted({t.name for t in self._channels[rows[0]].tags}))

    def set_channels(self, channels: list[ChannelOut]) -> None:
        if [c.id for c in channels] == [c.id for c in self._source_channels]:
            if any(self.needs_regroup(channel) for channel in channels):
                self._reset_channels(channels)
                return
            for channel in channels:
                old = self._source_channels[self._source_index[channel.id]]
                if old != channel:
                    self.update_channel(channel)
            return
        self._reset_channels(channels)

    def _reset_channels(self, channels):
        self.beginResetModel()
        self._display.clear()
        self._sort_keys.clear()
        self._search.clear()
        self._source_channels = list(channels)
        self._channels = []
        self.groups = []
        buckets = {}
        for channel in channels:
            self._cache_channel(channel)
            groups = sorted({tag.name for tag in channel.tags}) or ["No tags"]
            for group in groups if self.grouped else [""]:
                buckets.setdefault(group, []).append(channel)
        for group, members in buckets.items():
            self._channels.extend(members)
            self.groups.extend([group] * len(members))
        self._index_rows()
        self.endResetModel()

    def set_grouped(self, enabled):
        self.grouped = enabled
        self._reset_channels(self._source_channels)

    def set_uploaded_count(self, channel_id, count):
        if self.counts.get(str(channel_id)) == count:
            return
        self.counts[str(channel_id)] = count
        from uuid import UUID
        cid = channel_id if isinstance(channel_id, UUID) else UUID(str(channel_id))
        rows = self._rows.get(cid, ())
        if rows:
            self._cache_channel(self._channels[rows[0]])
        for row in rows:
            self.dataChanged.emit(self.index(row, 7), self.index(row, 7))

    def set_proxies(self, proxies: list[ProxyOut]) -> None:
        self._proxies = {proxy.id: proxy for proxy in proxies}
        for channel in self._source_channels:
            self._cache_channel(channel)
        if self._channels:
            column = len(self.COLUMNS) - 1
            self.dataChanged.emit(self.index(0, column), self.index(len(self._channels) - 1, column))

    def update_proxy(self, proxy: ProxyOut) -> None:
        self._proxies[proxy.id] = proxy
        column = len(self.COLUMNS) - 1
        for row, channel in enumerate(self._channels):
            if channel.proxy_id == proxy.id:
                self._cache_channel(channel)
                index = self.index(row, column)
                self.dataChanged.emit(index, index)

    def remove_proxy(self, proxy_id) -> None:
        proxy = next((item for item in self._proxies if str(item) == str(proxy_id)), None)
        if proxy is None:
            return
        del self._proxies[proxy]
        column = len(self.COLUMNS) - 1
        for row, channel in enumerate(self._channels):
            if channel.proxy_id == proxy:
                self._cache_channel(channel)
                index = self.index(row, column)
                self.dataChanged.emit(index, index)

    def _proxy_status(self, channel: ChannelOut) -> str:
        if channel.proxy_id is None:
            return "No proxy"
        proxy = self._proxies.get(channel.proxy_id)
        if proxy and (proxy.status == "working" or proxy.last_check_ok is True):
            return "Working"
        return "Not working"

    def update_channel(self, channel: ChannelOut) -> None:
        rows = self._rows.get(channel.id, ())
        if rows and not self.needs_regroup(channel):
            before = self._display[channel.id]
            self._source_channels[self._source_index[channel.id]] = channel
            self._cache_channel(channel)
            changed = [i for i, (a, b) in enumerate(zip(before, self._display[channel.id])) if a != b]
            for row in rows:
                self._channels[row] = channel
                if changed:
                    self.dataChanged.emit(self.index(row, min(changed)), self.index(row, max(changed)))
            return
        if self.grouped:
            channels = [channel if c.id == channel.id else c for c in self._source_channels]
            if not any(c.id == channel.id for c in channels):
                channels.append(channel)
            self._reset_channels(channels)
            return
        self._source_channels.append(channel)
        # New channel
        self.beginInsertRows(QModelIndex(), len(self._channels), len(self._channels))
        self._channels.append(channel)
        self.groups.append("")
        self._cache_channel(channel)
        self._index_rows()
        self.endInsertRows()

    def remove_channel(self, channel_id) -> None:
        self.set_channels([c for c in self._source_channels if c.id != channel_id])

    def get_channel(self, row: int) -> ChannelOut | None:
        if 0 <= row < len(self._channels):
            return self._channels[row]
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._channels)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLUMNS[section][0]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        channel = self._channels[row]
        count = self.counts.get(str(channel.id))
        if role == Qt.UserRole + 1:
            return self.groups[row] if col == 8 else self._sort_keys[channel.id][col]
        if role == Qt.BackgroundRole and col in (6, 7):
            return QColor(age_color(channel) if col == 6 else upload_color(count))
        if role == Qt.ForegroundRole and col in (6, 7):
            return QColor("#263238")
        if role == Qt.ToolTipRole:
            if col == 6:
                return f"{authorization_text(channel)}\nFirst successful authorization on this server. Blue: <7d; purple: 7–29d; teal: 30+d. Reauthorization does not reset this date."
            if col == 7:
                return "Server upload count. Gray: 0; blue: 1–99; teal: 100+. —: unavailable or loading."
            return str(self.data(index, Qt.DisplayRole))
        if role == Qt.DisplayRole:
            if col == 8:
                return self.groups[row]
            return self._display[channel.id][col]
        if role == Qt.UserRole:
            return channel
        if role == Qt.ForegroundRole and col == 2:
            colors = {
                "active": QColor("#2e7d32"),
                "paused": QColor("#ed6c02"),
                "error": QColor("#d32f2f"),
                "deleted": QColor("#757575"),
            }
            return colors.get(channel.status)
        if role == Qt.ForegroundRole and col == 3:
            colors = {
                "active": QColor("#2e7d32"),
                "expired": QColor("#ed6c02"),
                "reauth_required": QColor("#d32f2f"),
            }
            return colors.get(channel.session_status)
        if role == Qt.ForegroundRole and col == len(self.COLUMNS) - 1:
            colors = {
                "Working": QColor("#2e7d32"),
                "Not working": QColor("#c62828"),
                "No proxy": QColor("#757575"),
            }
            return colors[self._proxy_status(channel)]
        return None
