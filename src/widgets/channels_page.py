from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from PySide6.QtCore import QSortFilterProxyModel, Qt, QThread, Signal, QSignalBlocker, QItemSelection, QItemSelectionModel, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QScrollArea,
    QTableView,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QToolButton,
)

from src.api_client import ApiClient, ApiError
from src.api_worker import run_api
from src.models import (
    AICustomProviderOut,
    AITypeOut,
    ChannelBulkSettingsResult,
    ChannelOut,
    ChannelUploadedCount,
    FolderVideoOut,
    ProxyOut,
    TaskOut,
    UploadLimitOut,
)
from src.utils import format_local
from src.widgets.avatar_loader import AvatarLoader
from src.widgets.channel_table_model import ChannelTableModel, ChannelProxyModel, TagGroupProxyModel
from src.widgets.channel_labels import tag_text, authorization_text
from src.widgets.delay_input import DelayInputWidget

OVERVIEW_AVATAR_SIZE = 96

# Sentinel: the user has not made an explicit folder choice in the settings form.
_FOLDER_UNSET = object()

MAX_UPLOAD_ATTEMPTS = 5
RETRY_BASE_DELAY = 2.0  # секунды, экспоненциальная пауза между попытками
RETRY_MAX_DELAY = 60.0  # потолок паузы между ретраями, сек
# Параллельных загрузок. Сервер имеет memory backpressure (~3 файла в запись одновременно)
# и при большой конкурентности может отвечать 500 — но клиент их ретраит.
UPLOAD_WORKERS = 16


def _retry_delay(attempt: int) -> float:
    """Экспоненциальная задержка с джиттером, ограниченная RETRY_MAX_DELAY."""
    return min(RETRY_MAX_DELAY, RETRY_BASE_DELAY * (2 ** (attempt - 1))) + random.uniform(0, 1)


@dataclass
class FileUploadReport:
    file_name: str
    status: str  # "uploaded" | "skipped" | "error"
    attempts: int = 0
    errors: list[str] = field(default_factory=list)


class ParallelUploadThread(QThread):
    """Грузит файлы параллельно (пул потоков), с ретраями на каждый файл."""

    progress = Signal(object, int, int, str, int)  # channel_id, index, total, file_name, attempt
    file_done = Signal(object, object)  # channel_id, FileUploadReport
    all_done = Signal(object, object)  # channel_id, list[FileUploadReport]

    def __init__(self, api: ApiClient, channel_id, files: list[str], parent=None) -> None:
        super().__init__(parent)
        self.api = api
        self.channel_id = channel_id
        self.files = files

    @staticmethod
    def _is_skip(message: str) -> bool:
        lower = message.lower()
        return "already" in lower or "duplicate" in lower or "exists" in lower or "скип" in lower or "skip" in lower

    def _upload_one(self, idx: int, file_path: str) -> FileUploadReport:
        file_name = Path(file_path).name
        report = FileUploadReport(file_name=file_name, status="error")
        total = len(self.files)
        for attempt in range(1, MAX_UPLOAD_ATTEMPTS + 1):
            self.progress.emit(str(self.channel_id), idx, total, file_name, attempt)
            print(f"[Upload] start {idx}/{total} attempt {attempt}: {file_name}")
            try:
                result = self.api.upload_file(self.channel_id, file_path)
                report.attempts = attempt
                if result.skipped:
                    # Сервер отклонил файл (дубликат/уже загружен) — это не ошибка.
                    report.status = "skipped"
                    if result.reason:
                        report.errors.append(result.reason)
                    print(f"[Upload] skipped {idx}/{total}: {file_name} — {result.reason}")
                else:
                    report.status = "uploaded"
                    print(f"[Upload] done {idx}/{total}: {file_name}")
                break
            except ApiError as exc:
                message = exc.message
                report.errors.append(message)
                report.attempts = attempt
                # 4xx — ошибка запроса/валидации, повторять бессмысленно.
                if 400 <= exc.status_code < 500:
                    print(f"[Upload] client error {exc.status_code} {idx}/{total}: {file_name} — {message}")
                    break
                # 5xx — сбой сервера, имеет смысл подождать и повторить.
                if attempt < MAX_UPLOAD_ATTEMPTS:
                    delay = _retry_delay(attempt)
                    print(f"[Upload] server error {exc.status_code}, retry in {delay:.1f}s: {file_name}")
                    time.sleep(delay)
            except Exception as exc:  # noqa: BLE001 - сеть и прочие непредвиденные ошибки
                message = str(exc)
                if self._is_skip(message):
                    report.status = "skipped"
                    report.attempts = attempt
                    report.errors.append(message)
                    break
                report.errors.append(message)
                report.attempts = attempt
                if attempt < MAX_UPLOAD_ATTEMPTS:
                    delay = _retry_delay(attempt)
                    time.sleep(delay)
        return report

    def run(self) -> None:
        reports: list[FileUploadReport] = []
        cid = str(self.channel_id)
        with ThreadPoolExecutor(max_workers=min(UPLOAD_WORKERS, max(1, len(self.files)))) as pool:
            futures = [pool.submit(self._upload_one, idx, fp) for idx, fp in enumerate(self.files, start=1)]
            for fut in futures:
                report = fut.result()
                reports.append(report)
                self.file_done.emit(cid, report)
        self.all_done.emit(cid, reports)


class UploadReportDialog(QDialog):
    """Модальное окно с подробным отчётом о загрузке."""

    def __init__(self, reports: list[FileUploadReport], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Upload Report")
        self.setModal(True)
        self.resize(620, 420)

        uploaded = [r for r in reports if r.status == "uploaded"]
        skipped = [r for r in reports if r.status == "skipped"]
        failed = [r for r in reports if r.status == "error"]

        layout = QVBoxLayout(self)
        summary = QLabel(
            f"Total: {len(reports)} | Uploaded: {len(uploaded)} | "
            f"Skipped by server: {len(skipped)} | Failed: {len(failed)}"
        )
        summary.setStyleSheet("font-weight: bold;")
        layout.addWidget(summary)

        details = QTextEdit()
        details.setReadOnly(True)
        lines: list[str] = []
        for r in reports:
            if r.status == "uploaded":
                lines.append(f"[OK] {r.file_name} — uploaded (attempts: {r.attempts})")
            elif r.status == "skipped":
                reason = r.errors[-1] if r.errors else "skipped"
                lines.append(f"[SKIP] {r.file_name} — {reason}")
            else:
                lines.append(f"[ERROR] {r.file_name} — failed after {r.attempts} attempts")
                seen: dict[str, int] = {}
                for err in r.errors:
                    seen[err] = seen.get(err, 0) + 1
                for err, count in seen.items():
                    suffix = f" (x{count})" if count > 1 else ""
                    lines.append(f"    - {err}{suffix}")
        details.setPlainText("\n".join(lines))
        layout.addWidget(details)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ChannelFilesWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.api: ApiClient | None = None
        self.channel: ChannelOut | None = None

        layout = QVBoxLayout(self)
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self._table)

        btn_layout = QHBoxLayout()
        self._upload_btn = QPushButton("Upload Files")
        self._delete_btn = QPushButton("Delete Selected")
        self._loading_label = QLabel("")
        self._loading_label.setWordWrap(False)
        self._loading_label.setFixedWidth(360)
        btn_layout.addWidget(self._upload_btn)
        btn_layout.addWidget(self._delete_btn)
        btn_layout.addWidget(self._loading_label)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self._upload_btn.clicked.connect(self._upload_files)
        self._delete_btn.clicked.connect(self._delete_selected)

        self._files: list[FolderVideoOut] = []
        self._pending_deletes = 0
        # Состояние загрузки для каждого канала отдельно: str(channel_id) -> dict
        self._uploads: dict[str, dict] = {}
        self._status_text = ""
        self._status_max_chars = 60

    def set_api(self, api: ApiClient) -> None:
        self.api = api

    def _current_upload_state(self) -> dict | None:
        """Состояние загрузки выбранного канала, если она идёт."""
        if self.channel is None:
            return None
        return self._uploads.get(str(self.channel.id))

    def set_channel(self, channel: ChannelOut) -> None:
        self.channel = channel
        state = self._current_upload_state()
        # Показываем статус выбранного канала (или пусто, если для него нет загрузки).
        self._set_loading(state["status_text"] if state else "")
        self._load_files()

    def _set_loading(self, text: str) -> None:
        self._status_text = text
        self._loading_label.setText(self._shorten_status(text))
        self._upload_btn.setEnabled(not text)
        self._delete_btn.setEnabled(not text)

    def _refresh_status(self) -> None:
        """Восстанавливает текст статуса после внутренних обновлений списка."""
        if self._status_text:
            self._loading_label.setText(self._shorten_status(self._status_text))

    def _load_files(self) -> None:
        if not self.channel or not self.api:
            return
        # Не затираем статус активной загрузки при обновлении списка файлов.
        if self._current_upload_state() is None and self._pending_deletes <= 0:
            self._set_loading("Loading files...")
        channel_id = self.channel.id
        run_api(
            self.api,
            self.api.get_folder_videos,
            lambda files, cid=channel_id: self._on_files_loaded(cid, files),
            lambda message, cid=channel_id: self._on_files_error(cid, message),
            channel_id,
            parent=self,
        )

    def _on_files_loaded(self, channel_id, files: list[FolderVideoOut]) -> None:
        # Ответ мог прийти уже после переключения на другой канал.
        if self.channel is None or self.channel.id != channel_id:
            return
        self._files = files
        # Сбрасываем "Loading files..." только если не идёт загрузка/удаление.
        if self._current_upload_state() is None and self._pending_deletes <= 0:
            self._set_loading("")
        else:
            self._refresh_status()
        from PySide6.QtGui import QStandardItem, QStandardItemModel

        model = QStandardItemModel(len(self._files), 2)
        model.setHorizontalHeaderLabels(["File Name", "Size"])
        for row, f in enumerate(self._files):
            model.setItem(row, 0, QStandardItem(f.file_name))
            size_mb = f.size_bytes / (1024 * 1024)
            model.setItem(row, 1, QStandardItem(f"{size_mb:.2f} MB"))
        self._table.setModel(model)
        self._table.horizontalHeader().setStretchLastSection(True)

    def _on_files_error(self, channel_id, message: str) -> None:
        if self.channel is None or self.channel.id != channel_id:
            return
        if self._current_upload_state() is None and self._pending_deletes <= 0:
            self._set_loading("")
        else:
            self._refresh_status()
        QMessageBox.critical(self, "Error", f"Failed to load files: {message}")

    def _upload_files(self) -> None:
        if not self.channel or not self.api:
            return
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select Videos", "", "Videos (*.mp4 *.mov *.avi *.mkv *.webm *.flv *.wmv *.m4v *.mpeg *.mpg)"
        )
        if not files:
            return
        cid = str(self.channel.id)
        state = {
            "thread": None,
            "total": len(files),
            "finished": 0,
            "names": set(),
            "status_text": f"Uploading 0/{len(files)}...",
        }
        self._uploads[cid] = state
        thread = ParallelUploadThread(self.api, self.channel.id, files, parent=self)
        state["thread"] = thread
        thread.progress.connect(self._on_upload_progress)
        thread.file_done.connect(self._on_upload_file_done)
        thread.all_done.connect(self._on_upload_done)
        # ВАЖНО: finished привязываем к конкретному потоку, чтобы не трогать
        # состояние других каналов, и удаляем объект только после завершения.
        thread.finished.connect(lambda c=cid, t=thread: self._on_upload_thread_finished(c, t))
        self._set_loading(state["status_text"])
        thread.start()

    def _on_upload_progress(self, channel_id, idx: int, total: int, file_name: str, attempt: int) -> None:
        state = self._uploads.get(str(channel_id))
        if state is None:
            return
        state["names"].add(file_name)
        self._update_upload_status(str(channel_id), state)

    def _on_upload_file_done(self, channel_id, report: FileUploadReport) -> None:
        state = self._uploads.get(str(channel_id))
        if state is None:
            return
        state["names"].discard(report.file_name)
        state["finished"] += 1
        self._update_upload_status(str(channel_id), state)

    def _update_upload_status(self, cid: str, state: dict) -> None:
        total = state["total"]
        done = state["finished"]
        names = ", ".join(sorted(state["names"]))
        text = f"Uploading {done}/{total}"
        if names:
            text += f": {names}"
        state["status_text"] = text
        # Обновляем видимый лейбл только если сейчас выбран этот канал.
        if self.channel is not None and str(self.channel.id) == cid:
            self._set_loading(text)

    def _shorten_status(self, text: str) -> str:
        """Обрезает длинный статус с многоточием, чтобы не растягивать окно."""
        if len(text) <= self._status_max_chars:
            return text
        return text[: self._status_max_chars - 1].rstrip() + "…"

    def _on_upload_done(self, channel_id, reports: list[FileUploadReport]) -> None:
        cid = str(channel_id)
        state = self._uploads.get(cid)
        if state is not None:
            state["names"] = set()
        # Лейбл сбрасываем только если пользователь сейчас на этом канале.
        if self.channel is not None and str(self.channel.id) == cid:
            self._set_loading("")
        UploadReportDialog(reports, self).exec()
        if self.channel is not None and str(self.channel.id) == cid:
            self._load_files()

    def _on_upload_thread_finished(self, channel_id: str, thread) -> None:
        state = self._uploads.pop(str(channel_id), None)
        if state is not None:
            thread.deleteLater()

    def _delete_selected(self) -> None:
        if not self.channel or not self.api:
            return
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return
        model = self._table.model()
        file_names = [model.data(model.index(idx.row(), 0)) for idx in indexes]

        if len(file_names) == 1:
            msg = f"Delete '{file_names[0]}'?"
        else:
            msg = f"Delete {len(file_names)} files?"

        reply = QMessageBox.question(self, "Confirm", msg)
        if reply != QMessageBox.Yes:
            return

        self._pending_deletes = len(file_names)
        self._set_loading("Deleting...")
        for file_name in file_names:
            run_api(
                self.api,
                self.api.delete_file,
                lambda _, fn=file_name: self._delete_done(fn),
                self._on_delete_error,
                self.channel.id,
                file_name,
                parent=self,
            )

    def _delete_done(self, file_name: str) -> None:
        self._pending_deletes -= 1
        if self._pending_deletes <= 0:
            self._set_loading("")
            self._load_files()

    def _on_delete_error(self, message: str) -> None:
        self._pending_deletes -= 1
        if self._pending_deletes <= 0:
            self._set_loading("")
        QMessageBox.critical(self, "Error", f"Failed to delete: {message}")


class ChannelTasksWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.api: ApiClient | None = None
        self.channel: ChannelOut | None = None

        layout = QVBoxLayout(self)
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        layout.addWidget(self._table)
        self._refresh_btn = QPushButton("Refresh")
        layout.addWidget(self._refresh_btn)
        self._refresh_btn.clicked.connect(self._load)

        from src.widgets.task_table_model import TaskTableModel

        self._model = TaskTableModel(max_tasks=200)
        self._table.setModel(self._model)
        self._table.horizontalHeader().setStretchLastSection(True)

    def set_api(self, api: ApiClient) -> None:
        self.api = api

    def set_channel(self, channel: ChannelOut) -> None:
        self.channel = channel
        self._load()

    def _load(self) -> None:
        if not self.channel or not self.api:
            return
        channel_id = self.channel.id
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("Refreshing...")
        run_api(
            self.api,
            self.api.get_tasks,
            lambda tasks, cid=channel_id: self._on_tasks_loaded(cid, tasks),
            lambda message, cid=channel_id: self._on_tasks_error(cid, message),
            channel_id=channel_id,
            limit=200,
            parent=self,
        )

    def _on_tasks_loaded(self, channel_id, tasks) -> None:
        # Ответ мог прийти уже после переключения на другой канал.
        if self.channel is None or self.channel.id != channel_id:
            return
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("Refresh")
        self._model.set_tasks(tasks)

    def _on_tasks_error(self, channel_id, message: str) -> None:
        if self.channel is None or self.channel.id != channel_id:
            return
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("Refresh")
        QMessageBox.critical(self, "Error", f"Failed to load tasks: {message}")

    def update_tasks(self, tasks: list[TaskOut]) -> None:
        print(f"[ChannelTasksWidget] update_tasks count={len(tasks)}")
        self._model.update_tasks(tasks)

    def update_task(self, task: TaskOut) -> None:
        print(f"[ChannelTasksWidget] update_task task.id={task.id}")
        self._model.update_task(task)

    def remove_task(self, task_id) -> None:
        print(f"[ChannelTasksWidget] remove_task task_id={task_id}")
        self._model.remove_task(task_id)

    def add_tasks(self, tasks: list[TaskOut]) -> None:
        for t in tasks:
            self._model.update_task(t)


class TagChannelList(QWidget):
    """One collapsible, independently selectable tag group."""

    selection_changed = Signal(object)
    expanded = Signal(object, bool)

    def __init__(self, tag_name, channel_ids, source_model, parent=None):
        super().__init__(parent)
        self.tag_name = tag_name
        self.channel_ids = set(channel_ids)
        self._proxy = TagGroupProxyModel(channel_ids, self)
        self._proxy.setSourceModel(source_model)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        controls = QHBoxLayout()
        self._toggle = QToolButton()
        self._toggle.setCheckable(True)
        self._toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._toggle.setText(f"Tag: {tag_name} ({len(channel_ids)})")
        self._toggle.toggled.connect(self._set_expanded)
        self._select_all = QPushButton("Select all")
        self._deselect_all = QPushButton("Deselect all")
        self._select_all.clicked.connect(self._table_select_all)
        self._deselect_all.clicked.connect(self.clear_selection)
        controls.addWidget(self._toggle, 1)
        controls.addWidget(self._select_all)
        controls.addWidget(self._deselect_all)
        layout.addLayout(controls)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setWordWrap(False)
        self._table.setSortingEnabled(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        row_height = self._table.fontMetrics().height() + 8
        self._table.verticalHeader().setDefaultSectionSize(row_height)
        # An expanded group should show its channels, not consume all available space.
        self._table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._table.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._visible_rows = min(len(channel_ids), 24)
        self._fit_table_height()
        for column in (3, 4, 8, 9):
            self._table.setColumnHidden(column, True)
        for column, width in ((0, 160), (1, 90), (2, 75), (5, 160), (7, 70), (6, 80)):
            self._table.setColumnWidth(column, width)
        self._table.selectionModel().selectionChanged.connect(lambda *_: self.selection_changed.emit(self))
        layout.addWidget(self._table)
        self._set_expanded(False)

    def _fit_table_height(self):
        """Fit header, visible rows, borders and an active horizontal scrollbar."""
        header_height = self._table.horizontalHeader().height() or self._table.horizontalHeader().sizeHint().height()
        scrollbar_height = self._table.horizontalScrollBar().sizeHint().height() if self._table.horizontalScrollBar().isVisible() else 0
        self._table.setFixedHeight(
            header_height
            + self._table.verticalHeader().sectionSize(0) * self._visible_rows
            + scrollbar_height
            + self._table.frameWidth() * 2
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._fit_table_height)

    def _set_expanded(self, expanded):
        self._table.setVisible(expanded)
        self._select_all.setEnabled(expanded)
        self._deselect_all.setEnabled(expanded)
        self._toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        if expanded:
            QTimer.singleShot(0, self._fit_table_height)
        self.expanded.emit(self, expanded)

    def set_expanded(self, expanded):
        with QSignalBlocker(self._toggle):
            self._toggle.setChecked(expanded)
        self._set_expanded(expanded)

    def _table_select_all(self):
        self._table.selectAll()

    def clear_selection(self):
        self._table.clearSelection()

    def selected_channels(self):
        channels = []
        for index in self._table.selectionModel().selectedRows():
            source = self._proxy.mapToSource(index)
            channel = self._proxy.sourceModel().get_channel(source.row())
            if channel is not None:
                channels.append(channel)
        return channels

    def apply_sort(self, column, order):
        self._proxy.sort(column, order)


class TagGroupsWidget(QScrollArea):
    """A set of tag lists; only one list may own the selection at a time."""

    selection_changed = Signal()

    def __init__(self, source_model, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._source_model = source_model
        self._panels = []
        self._active = None
        self._restoring_selection = False
        self._signature = None
        self._group_order = []
        self._sort = (-1, Qt.AscendingOrder)
        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._layout.addStretch()
        self.setWidget(self._content)

    def set_channels(self, channels):
        signature = tuple((c.id, tuple(tag.name for tag in c.tags)) for c in channels)
        if signature == self._signature:
            return
        selected = [c.id for c in self.selected_channels()]
        active_name = self._active.tag_name if self._active else None
        self._signature = signature
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._panels = []
        self._active = None
        groups = {}
        for channel in channels:
            names = [tag.name for tag in channel.tags] or ["No tags"]
            for name in names:
                groups.setdefault(name, []).append(channel.id)
        existing = [name for name in self._group_order if name in groups and name != "No tags"]
        new = [name for name in groups if name not in self._group_order and name != "No tags"]
        self._group_order = (["No tags"] if "No tags" in groups else []) + existing + new
        for name in self._group_order:
            ids = groups[name]
            panel = TagChannelList(name, ids, self._source_model, self._content)
            panel.selection_changed.connect(self._on_panel_selection)
            panel.expanded.connect(self._on_panel_expanded)
            panel.apply_sort(*self._sort)
            self._layout.insertWidget(self._layout.count() - 1, panel)
            self._panels.append(panel)
            if name == active_name:
                self._active = panel
        if self._active:
            self._active.set_expanded(True)
            wanted = set(selected)
            selection = QItemSelection()
            for row in range(self._active._proxy.rowCount()):
                index = self._active._proxy.index(row, 0)
                source = self._active._proxy.mapToSource(index)
                channel = self._source_model.get_channel(source.row())
                if channel and channel.id in wanted:
                    selection.select(index, self._active._proxy.index(index.row(), self._active._proxy.columnCount() - 1))
            with QSignalBlocker(self._active._table.selectionModel()):
                self._active._table.selectionModel().select(selection, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)

    def _activate(self, panel):
        if self._active is panel:
            return
        if self._active:
            with QSignalBlocker(self._active._table.selectionModel()):
                self._active.clear_selection()
            self._active.set_expanded(False)
        self._active = panel
        panel.set_expanded(True)

    def _on_panel_expanded(self, panel, expanded):
        if self._restoring_selection:
            return
        if expanded:
            self._activate(panel)
            self.selection_changed.emit()
        elif self._active is panel:
            with QSignalBlocker(panel._table.selectionModel()):
                panel.clear_selection()
            self._active = None
            self.selection_changed.emit()

    def _on_panel_selection(self, panel):
        if self._restoring_selection:
            return
        if panel.selected_channels():
            self._activate(panel)
        self.selection_changed.emit()

    def selected_channels(self):
        return self._active.selected_channels() if self._active else []

    def select_all_active(self):
        if self._active:
            self._active._table_select_all()

    def select_channels(self, channel_ids):
        """Restore a selection only when one tag list contains every channel."""
        wanted = set(channel_ids)
        if not wanted:
            return False
        panel = next((item for item in self._panels if wanted.issubset(item.channel_ids)), None)
        if panel is None:
            return False
        self._restoring_selection = True
        try:
            self._activate(panel)
            selection = QItemSelection()
            for row in range(panel._proxy.rowCount()):
                index = panel._proxy.index(row, 0)
                source = panel._proxy.mapToSource(index)
                channel = self._source_model.get_channel(source.row())
                if channel and channel.id in wanted:
                    selection.select(index, panel._proxy.index(index.row(), panel._proxy.columnCount() - 1))
            with QSignalBlocker(panel._table.selectionModel()):
                panel._table.selectionModel().select(selection, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        finally:
            self._restoring_selection = False
        return True

    def all_active_selected(self):
        return bool(self._active and self._active._proxy.rowCount() and len(self._active.selected_channels()) == self._active._proxy.rowCount())

    def apply_sort(self, column, order):
        self._sort = (column, order)
        for panel in self._panels:
            panel.apply_sort(column, order)


class ChannelsPage(QWidget):
    uploaded_count_changed = Signal(object, object)
    tags_changed = Signal(object)
    channel_auth_requested = Signal(object)  # ChannelOut or None
    channel_deleted = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.api: ApiClient | None = None
        self._avatar_loader: AvatarLoader | None = None
        self._current_channel: ChannelOut | None = None
        self._current_uploaded_count: ChannelUploadedCount | None = None
        self._uploaded_count_failed = False
        self._count_queue = []
        self._count_inflight = set()
        self._count_generation = 0
        self._tag_loading = False
        self._refresh_counts_snapshot = False
        self._all_channels: list[ChannelOut] = []
        self._proxies: list[ProxyOut] = []
        self._ai_types: list[AITypeOut] = []
        self._ai_providers: list[AICustomProviderOut] = []
        # Автоподбор прокси: счётчик поколений запросов, поколение запроса в
        # работе и вручную выбранный прокси для отменённых подборов.
        self._auto_proxy_seq: dict[UUID, int] = {}
        self._auto_proxy_pending: dict[UUID, int] = {}
        self._auto_proxy_manual: dict[UUID, tuple[str | None, bool]] = {}
        # Состояние массового сохранения настроек выбранных каналов.
        self._bulk_save_count = 0

        layout = QHBoxLayout(self)
        self._splitter = QSplitter(Qt.Horizontal)
        layout.addWidget(self._splitter)

        # Left panel
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)

        search_layout = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search by title, handle, status, tags...")
        self._search.setClearButtonEnabled(True)
        self._select_all_btn = QPushButton("Select All")
        self._select_all_btn.clicked.connect(self._toggle_select_all)
        search_layout.addWidget(self._search, 1)
        search_layout.addWidget(self._select_all_btn)
        left_layout.addLayout(search_layout)

        self._channel_table = QTableView()
        self._channel_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._channel_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._channel_table.horizontalHeader().setStretchLastSection(True)
        self._channel_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)

        self._channel_model = ChannelTableModel()
        self._proxy_model = ChannelProxyModel()
        self._proxy_model.setSourceModel(self._channel_model)
        self._proxy_model.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._proxy_model.setFilterKeyColumn(-1)
        self._channel_table.setModel(self._proxy_model)
        self._channel_table.setColumnHidden(8, True)
        header = self._channel_table.horizontalHeader()
        for position, column in enumerate((0, 1, 2, 3, 4, 9, 5, 8, 7, 6)):
            header.moveSection(header.visualIndex(column), position)
        for column, width in ((0, 150), (1, 95), (8, 90), (5, 150), (7, 70), (6, 80)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            header.resizeSection(column, width)
        self._channel_table.setWordWrap(False)
        self._channel_table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        row_height = self._channel_table.fontMetrics().height() + 8
        self._channel_table.verticalHeader().setMinimumSectionSize(row_height)
        self._channel_table.verticalHeader().setDefaultSectionSize(row_height)
        self._channel_table.setSortingEnabled(False)
        self._channel_table.horizontalHeader().setSortIndicatorShown(False)
        controls = QHBoxLayout()
        self._sort = QComboBox()
        for label, column, order in [("No sorting (server order)", -1, Qt.AscendingOrder), ("Title A–Z", 0, Qt.AscendingOrder), ("Authorization: newest first", 6, Qt.DescendingOrder), ("Authorization: oldest first", 6, Qt.AscendingOrder), ("Uploaded: most first", 7, Qt.DescendingOrder), ("Uploaded: fewest first", 7, Qt.AscendingOrder)]:
            self._sort.addItem(label, (column, order))
        self._sort.setPlaceholderText("Custom column sorting")
        self._sort.currentIndexChanged.connect(self._apply_sort)
        header.sortIndicatorChanged.connect(self._sync_sort_choice)
        self._group_tags = QCheckBox("Group by tags")
        self._group_tags.setToolTip("Channels with several tags appear in each group. Without sorting, server order is preserved within each group. Bulk actions apply once per channel.")
        self._group_tags.toggled.connect(self._set_tag_grouping)
        controls.addWidget(self._sort)
        controls.addWidget(self._group_tags)
        left_layout.addLayout(controls)
        self._channel_table.setColumnWidth(len(ChannelTableModel.COLUMNS) - 1, 105)
        self._channel_table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self._channel_table)
        self._tag_groups = TagGroupsWidget(self._channel_model)
        self._tag_groups.selection_changed.connect(self._on_selection_changed)
        self._tag_groups.hide()
        left_layout.addWidget(self._tag_groups)

        self._splitter.addWidget(left)

        # Right panel
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()
        self._overview_tab = QWidget()
        self._settings_tab = QWidget()
        self._files_tab = ChannelFilesWidget()
        self._tasks_tab = ChannelTasksWidget()

        self._build_overview()
        self._build_settings()
        self._build_bulk_settings()

        self._tabs.addTab(self._overview_tab, "Overview")
        self._tabs.addTab(self._settings_tab, "Settings")
        self._tabs.addTab(self._files_tab, "Files")
        self._tabs.addTab(self._tasks_tab, "Tasks")
        right_layout.addWidget(self._tabs)
        right_layout.addWidget(self._bulk_widget)
        self._bulk_widget.hide()

        self._splitter.addWidget(right)
        # Leave enough room for the full proxy status text in the channel list.
        self._splitter.setSizes([720, 560])

        self._search.textChanged.connect(self._proxy_model.setFilterFixedString)
        self._search.textChanged.connect(lambda *_: self._refresh_select_all_button())

    def set_api(self, api: ApiClient) -> None:
        self._count_generation += 1
        self._refresh_counts_snapshot = True
        self._count_queue.clear()
        self._count_inflight.clear()
        self._tag_loading = False
        self.api = api
        self._files_tab.set_api(api)
        self._tasks_tab.set_api(api)
        self._avatar_loader = AvatarLoader(api, self)
        self._avatar_loader.avatar_loaded.connect(self._on_avatar_loaded)

    def _build_overview(self) -> None:
        layout = QVBoxLayout(self._overview_tab)

        header = QHBoxLayout()
        header.setSpacing(10)
        header.setContentsMargins(0, 0, 0, 0)
        header.setAlignment(Qt.AlignTop)
        self._ov_avatar = QLabel()
        self._ov_avatar.setFixedSize(OVERVIEW_AVATAR_SIZE, OVERVIEW_AVATAR_SIZE)
        self._ov_avatar.setAlignment(Qt.AlignCenter)
        self._ov_avatar.setStyleSheet(
            "background-color: #e0e0e0; border-radius: 8px;"
        )
        header.addWidget(self._ov_avatar, 0, Qt.AlignTop)

        title_layout = QVBoxLayout()
        title_layout.setSpacing(2)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setAlignment(Qt.AlignTop)
        self._ov_title = QLabel("<h2 style='margin:0;'>Select a channel</h2>")
        self._ov_title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._ov_handle = QLabel("")
        self._ov_handle.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        title_layout.addWidget(self._ov_title)
        title_layout.addWidget(self._ov_handle)

        title_container = QWidget()
        title_container.setLayout(title_layout)
        title_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        header.addWidget(title_container, 1, Qt.AlignTop)
        layout.addLayout(header)
        self._ov_tags = QLabel()
        self._ov_tags.setTextFormat(Qt.PlainText)
        self._ov_tags.setWordWrap(True)
        layout.addWidget(self._ov_tags)
        self._ov_authorized = QLabel()
        layout.addWidget(self._ov_authorized)
        tag_row = QHBoxLayout()
        self._tag_combo = QComboBox()
        self._tag_combo.setEditable(True)
        self._tag_combo.setInsertPolicy(QComboBox.NoInsert)
        self._tag_combo.lineEdit().setMaxLength(100)
        self._tag_combo.lineEdit().setPlaceholderText("Select or enter a tag")
        self._tag_add = QPushButton("Add tag")
        self._tag_remove = QPushButton("Remove tag")
        self._tag_reload = QPushButton("Refresh tags")
        self._tag_add.clicked.connect(lambda: self._change_tag(True))
        self._tag_remove.clicked.connect(lambda: self._change_tag(False))
        self._tag_reload.clicked.connect(self._load_tags)
        for widget in (self._tag_combo, self._tag_add, self._tag_remove, self._tag_reload):
            tag_row.addWidget(widget)
        layout.addLayout(tag_row)

        self._ov_status = QLabel("")
        self._ov_session = QLabel("")
        self._ov_subscribers = QLabel("")
        self._ov_views = QLabel("")
        self._ov_metadata_updated = QLabel("")
        self._ov_folder = QLabel("")
        self._ov_auto = QLabel("")
        self._ov_paused_until = QLabel("")
        self._ov_pause_reason = QLabel("")
        self._ov_uploaded = QLabel("")
        self._ov_uploaded.setWordWrap(True)

        info = QFormLayout()
        info.addRow("Status:", self._ov_status)
        info.addRow("Session:", self._ov_session)
        info.addRow("Subscribers:", self._ov_subscribers)
        info.addRow("Views:", self._ov_views)
        info.addRow("Metadata updated:", self._ov_metadata_updated)
        info.addRow("Folder:", self._ov_folder)
        info.addRow("Auto Upload:", self._ov_auto)
        info.addRow("Paused until:", self._ov_paused_until)
        info.addRow("Pause reason:", self._ov_pause_reason)
        uploaded_row = QHBoxLayout()
        uploaded_row.setSpacing(6)
        uploaded_row.addWidget(QLabel("Total uploaded:"))
        uploaded_row.addWidget(self._ov_uploaded)
        uploaded_row.addStretch()
        info.addRow(uploaded_row)
        layout.addLayout(info)

        btn_layout = QHBoxLayout()
        self._btn_unpause = QPushButton("Unpause")
        self._btn_clear_error = QPushButton("Clear Error")
        self._btn_reauth = QPushButton("Re-authorize")
        self._btn_delete = QPushButton("Delete Channel")
        btn_layout.addWidget(self._btn_unpause)
        btn_layout.addWidget(self._btn_clear_error)
        btn_layout.addWidget(self._btn_reauth)
        btn_layout.addWidget(self._btn_delete)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self._btn_unpause.clicked.connect(self._unpause)
        self._btn_clear_error.clicked.connect(self._clear_error)
        self._btn_reauth.clicked.connect(self._reauth)
        self._btn_delete.clicked.connect(self._delete_channel)

        layout.addStretch()

    def _build_settings(self) -> None:
        layout = QVBoxLayout(self._settings_tab)
        form = QFormLayout()
        self._set_title = QLineEdit()
        self._set_title_template = QLineEdit()
        self._set_title_template.setPlaceholderText("{filename} - my channel")
        self._set_desc_template = QTextEdit()
        self._set_desc_template.setMaximumHeight(120)
        self._set_privacy = QComboBox()
        self._set_privacy.addItems(["public", "unlisted", "private"])
        self._set_category = QLineEdit()
        self._set_auto = QCheckBox("Enabled")
        self._set_save = QPushButton("Save Settings")
        self._set_proxy = QComboBox()
        self._set_proxy_auto = QCheckBox("Switch automatically after a proxy failure")
        self._set_proxy_auto_select = QPushButton("Auto-select working proxy")
        self._set_proxy_auto_select.clicked.connect(self._auto_select_channel_proxy)
        self._set_proxy_last = QPushButton("Set last used proxy")
        self._set_proxy_last.clicked.connect(self._set_last_used_proxy)
        self._set_proxy_browser = QPushButton("Open Browser")
        self._set_proxy_browser.clicked.connect(self._open_channel_browser)
        self._set_storage_state = QPushButton("Set last storage state")
        self._set_storage_state.clicked.connect(self._set_last_storage_state)

        self._set_folder_label = QLabel("Dedicated folder")
        self._set_folder_btn = QPushButton("Change...")
        self._set_folder_btn.clicked.connect(self._pick_folder_channel)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self._set_folder_label, 1)
        folder_row.addWidget(self._set_folder_btn)

        form.addRow("Title:", self._set_title)
        form.addRow("Video Title Template:", self._set_title_template)
        form.addRow("Description Template:", self._set_desc_template)
        form.addRow("Privacy:", self._set_privacy)
        form.addRow("Category ID:", self._set_category)
        form.addRow("Video folder:", folder_row)
        form.addRow(self._set_auto)
        form.addRow("Proxy:", self._set_proxy)
        form.addRow("Proxy failover:", self._set_proxy_auto)
        form.addRow(self._set_proxy_auto_select)
        form.addRow(self._set_proxy_last, self._set_proxy_browser)
        form.addRow("Storage state:", self._set_storage_state)
        layout.addLayout(form)

        # Batch upload settings
        batch_group = QGroupBox("Batch Upload")
        batch_layout = QFormLayout(batch_group)
        self._set_max_batch = QSpinBox()
        self._set_max_batch.setRange(1, 200)
        self._set_min_batch = QSpinBox()
        self._set_min_batch.setRange(1, 200)
        self._set_inter_delay = DelayInputWidget()
        self._set_shuffle_order = QCheckBox("Shuffle upload order")
        self._set_skip_blocked_videos = QCheckBox("Skip blocked videos")
        batch_layout.addRow("Max batch size:", self._set_max_batch)
        batch_layout.addRow("Min batch size:", self._set_min_batch)
        batch_layout.addRow("Inter-upload delay:", self._set_inter_delay)
        batch_layout.addRow(self._set_shuffle_order)
        batch_layout.addRow(self._set_skip_blocked_videos)
        layout.addWidget(batch_group)

        # AI metadata generation settings
        ai_group = QGroupBox("AI Metadata Generation")
        ai_layout = QFormLayout(ai_group)
        self._set_ai_model = QComboBox()
        self._set_ai_generate_description = QCheckBox("Generate description with the description prompt")
        self._set_ai_use_cached = QCheckBox("Reuse cached generations when available")
        ai_layout.addRow("AI model:", self._set_ai_model)
        ai_layout.addRow(self._set_ai_generate_description)
        ai_layout.addRow(self._set_ai_use_cached)
        layout.addWidget(ai_group)

        # Кнопка сохранения — под блоком Batch Upload.
        layout.addWidget(self._set_save)

        # Upload limit section
        ul_group = QGroupBox("Upload Limit")
        ul_layout = QFormLayout(ul_group)
        self._set_ul_enabled = QCheckBox("Enabled")
        self._set_ul_count = QSpinBox()
        self._set_ul_count.setRange(1, 10000)
        self._set_ul_period = QSpinBox()
        self._set_ul_period.setRange(1, 720)
        self._set_ul_period.setSuffix(" h")
        self._set_ul_status = QLabel("")
        self._set_ul_status.setWordWrap(True)
        self._set_ul_save = QPushButton("Save Upload Limit")
        ul_layout.addRow(self._set_ul_enabled)
        ul_layout.addRow("Max uploads:", self._set_ul_count)
        ul_layout.addRow("Period:", self._set_ul_period)
        ul_layout.addRow("Status:", self._set_ul_status)
        ul_layout.addRow(self._set_ul_save)
        layout.addWidget(ul_group)
        layout.addStretch()

        self._folder_from_channel_id: str | None = None
        # Tri-state explicit folder choice made in the form:
        #   _FOLDER_UNSET — the user has not touched the folder row;
        #   None — the user explicitly wants a dedicated folder;
        #   "<uuid>" — the user explicitly wants the referenced channel's folder.
        self._folder_pick: object = _FOLDER_UNSET
        self._set_save.clicked.connect(self._save_settings)
        self._set_ul_save.clicked.connect(self._save_upload_limit)

        self._current_upload_limit: UploadLimitOut | None = None

    def _build_bulk_settings(self) -> None:
        """Панель настроек, показываемая при выделении нескольких каналов."""
        self._bulk_widget = QWidget()
        layout = QVBoxLayout(self._bulk_widget)
        layout.setContentsMargins(12, 12, 12, 12)

        self._bulk_title = QLabel()
        layout.addWidget(self._bulk_title)

        self._bulk_tabs = QTabWidget()

        # --- Вкладка 1: существующие настройки (batch upload / upload limit / proxy failover)
        settings_tab = QWidget()
        settings_layout = QVBoxLayout(settings_tab)
        settings_layout.setContentsMargins(0, 0, 0, 0)

        batch_group = QGroupBox("Batch Upload")
        batch_layout = QFormLayout(batch_group)
        self._bulk_max_batch = QSpinBox()
        self._bulk_max_batch.setRange(1, 200)
        self._bulk_min_batch = QSpinBox()
        self._bulk_min_batch.setRange(1, 200)
        self._bulk_inter_delay = DelayInputWidget()
        self._bulk_shuffle = QCheckBox("Shuffle upload order")
        self._bulk_skip_blocked_videos = QCheckBox("Skip blocked videos")
        batch_layout.addRow("Max batch size:", self._bulk_max_batch)
        batch_layout.addRow("Min batch size:", self._bulk_min_batch)
        batch_layout.addRow("Inter-upload delay:", self._bulk_inter_delay)
        batch_layout.addRow(self._bulk_shuffle)
        batch_layout.addRow(self._bulk_skip_blocked_videos)
        settings_layout.addWidget(batch_group)

        ul_group = QGroupBox("Upload Limit")
        ul_layout = QFormLayout(ul_group)
        self._bulk_ul_enabled = QCheckBox("Enabled")
        self._bulk_ul_count = QSpinBox()
        self._bulk_ul_count.setRange(1, 10000)
        self._bulk_ul_period = QSpinBox()
        self._bulk_ul_period.setRange(1, 720)
        self._bulk_ul_period.setSuffix(" h")
        ul_layout.addRow(self._bulk_ul_enabled)
        ul_layout.addRow("Max uploads:", self._bulk_ul_count)
        ul_layout.addRow("Period:", self._bulk_ul_period)
        settings_layout.addWidget(ul_group)

        ai_group = QGroupBox("AI Metadata Generation")
        ai_layout = QFormLayout(ai_group)
        self._bulk_ai_model = QComboBox()
        self._bulk_ai_generate_description = QCheckBox("Generate description with the description prompt")
        self._bulk_ai_use_cached = QCheckBox("Reuse cached generations when available")
        ai_layout.addRow("AI model:", self._bulk_ai_model)
        ai_layout.addRow(self._bulk_ai_generate_description)
        ai_layout.addRow(self._bulk_ai_use_cached)
        settings_layout.addWidget(ai_group)

        self._bulk_proxy_auto = QCheckBox("Switch automatically after a proxy failure")
        settings_layout.addWidget(self._bulk_proxy_auto)

        self._bulk_save = QPushButton("Save Settings")
        self._bulk_save.clicked.connect(self._save_bulk_settings)
        settings_layout.addWidget(self._bulk_save)

        settings_layout.addStretch()
        self._bulk_tabs.addTab(settings_tab, "Settings")

        # --- Вкладка 2: настройки видео (название/шаблон описания/приватность/категория)
        video_tab = QWidget()
        video_layout = QVBoxLayout(video_tab)
        video_layout.setContentsMargins(12, 12, 12, 12)
        video_form = QFormLayout()

        self._bulk_video_title_template = QLineEdit()
        self._bulk_video_title_template.setPlaceholderText("{filename} - my channel")
        self._bulk_video_desc_template = QTextEdit()
        self._bulk_video_desc_template.setMaximumHeight(120)
        self._bulk_video_privacy = QComboBox()
        self._bulk_video_privacy.addItems(["public", "unlisted", "private"])
        self._bulk_video_category = QLineEdit()
        video_form.addRow("Video Title Template:", self._bulk_video_title_template)
        video_form.addRow("Description Template:", self._bulk_video_desc_template)
        video_form.addRow("Privacy:", self._bulk_video_privacy)
        video_form.addRow("Category ID:", self._bulk_video_category)
        video_layout.addLayout(video_form)

        self._bulk_video_save = QPushButton("Save Video Settings")
        self._bulk_video_save.clicked.connect(self._save_bulk_video_settings)
        video_layout.addWidget(self._bulk_video_save)

        video_layout.addStretch()
        self._bulk_tabs.addTab(video_tab, "Video")

        # --- Вкладка 3: video folder (общая папка с другим каналом / dedicated)
        folder_tab = QWidget()
        folder_layout = QVBoxLayout(folder_tab)
        folder_layout.setContentsMargins(12, 12, 12, 12)
        folder_form = QFormLayout()

        self._bulk_folder_label = QLabel("Dedicated folder for each channel")
        self._bulk_folder_label.setWordWrap(True)
        self._bulk_folder_btn = QPushButton("Select...")
        self._bulk_folder_btn.clicked.connect(self._pick_bulk_folder_channel)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self._bulk_folder_label, 1)
        folder_row.addWidget(self._bulk_folder_btn)
        folder_form.addRow("Copy folder from:", folder_row)
        folder_layout.addLayout(folder_form)

        self._bulk_folder_save = QPushButton("Save Folder Settings")
        self._bulk_folder_save.clicked.connect(self._save_bulk_folder_settings)
        folder_layout.addWidget(self._bulk_folder_save)

        folder_layout.addStretch()
        self._bulk_tabs.addTab(folder_tab, "Folder")
        self._bulk_folder_pick: object = _FOLDER_UNSET

        layout.addWidget(self._bulk_tabs)

    def set_channels(self, channels: list[ChannelOut]) -> None:
        previous_ids = {c.id for c in self._all_channels}
        self._all_channels = list(channels)
        selected = {c.id for c in self._selected_channels()}
        with QSignalBlocker(self._channel_table.selectionModel()):
            self._channel_model.set_channels(channels)
            self._restore_channel_selection(selected)
        if self._group_tags.isChecked():
            self._tag_groups.set_channels(channels)
        self._on_selection_changed()
        self._refresh_select_all_button()
        if not previous_ids or self._refresh_counts_snapshot:
            self._load_tags()
        self._count_queue.extend(c.id for c in channels if c.status != "deleted" and (self._refresh_counts_snapshot or c.id not in previous_ids) and c.id not in self._count_queue)
        self._refresh_counts_snapshot = False
        self._next_count()

    def _apply_sort(self):
        choice = self._sort.currentData()
        if choice is not None:
            column, order = choice
            header = self._channel_table.horizontalHeader()
            if column == -1:
                self._channel_table.setSortingEnabled(False)
                self._proxy_model.sort(-1, order)
                header.setSortIndicatorShown(False)
            else:
                self._channel_table.setSortingEnabled(True)
                header.setSortIndicatorShown(True)
                self._channel_table.sortByColumn(column, order)
            self._tag_groups.apply_sort(column, order)

    def _sync_sort_choice(self, column, order):
        with QSignalBlocker(self._sort):
            match = next((i for i in range(self._sort.count()) if self._sort.itemData(i) == (column, order) or (column == -1 and self._sort.itemData(i)[0] == -1)), -1)
            self._sort.setCurrentIndex(match)

    def _set_tag_grouping(self, enabled):
        selected = self._selected_table_channels() if enabled else self._tag_groups.selected_channels()
        if enabled:
            with QSignalBlocker(self._channel_table.selectionModel()):
                self._channel_table.clearSelection()
            self._tag_groups.set_channels(self._all_channels)
            self._channel_table.hide()
            self._tag_groups.show()
            self._tag_groups.select_channels(channel.id for channel in selected)
        else:
            selected_ids = {channel.id for channel in selected}
            with QSignalBlocker(self._channel_table.selectionModel()):
                self._restore_channel_selection(selected_ids)
            self._tag_groups.hide()
            self._channel_table.show()
        self._on_selection_changed()

    def _restore_channel_selection(self, selected):
        if not selected:
            return
        selection = QItemSelection()
        for cid in selected:
            for row in self._channel_model.rows_for_channel(cid):
                index = self._proxy_model.mapFromSource(self._channel_model.index(row, 0))
                if index.isValid():
                    selection.select(index, self._proxy_model.index(index.row(), self._proxy_model.columnCount() - 1))
        self._channel_table.selectionModel().select(selection, QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)

    def _load_tags(self):
        if self.api and not self._tag_loading:
            self._tag_loading = True
            api = self.api
            def done(tags):
                if self.api is api:
                    self._tag_loading = False
                    self._on_tags_loaded(tags)
            def failed(message):
                if self.api is api:
                    self._tag_loading = False
                    self._tag_combo.setToolTip(message)
            run_api(api, api.get_tags, done, failed, parent=self)

    def _on_tags_loaded(self, tags):
        text = self._tag_combo.currentText()
        self._tag_combo.clear()
        for tag in tags:
            self._tag_combo.addItem(tag.name, tag.id)
        self._tag_combo.setEditText(text)
        self._tag_combo.setToolTip("")

    def _change_tag(self, add):
        channel = self._current_channel
        name = self._tag_combo.currentText().strip()
        if not self.api or not channel or not name:
            return
        assigned_tag = next((tag for tag in channel.tags if tag.name == name), None)
        catalog_index = next((
            index for index in range(self._tag_combo.count())
            if self._tag_combo.itemText(index) == name
        ), -1)
        catalog_tag_id = self._tag_combo.itemData(catalog_index) if catalog_index >= 0 else None
        if add and assigned_tag is not None:
            return
        if not add and assigned_tag is None:
            QMessageBox.information(self, "Tags", "This tag is not assigned to this channel.")
            return
        api = self.api
        created_catalog_tag = add and catalog_tag_id is None

        def change():
            if add:
                tag_id = catalog_tag_id
                if tag_id is None:
                    tag_id = api.create_tag(name).id
                return api.assign_channel_tag(channel.id, tag_id)
            return api.remove_channel_tag(channel.id, assigned_tag.id)

        self._tag_add.setEnabled(False)
        self._tag_remove.setEnabled(False)
        button = self._tag_add if add else self._tag_remove
        original_text = button.text()
        button.setText("Adding…" if add else "Removing…")

        def done(updated):
            self._tag_add.setEnabled(True)
            self._tag_remove.setEnabled(True)
            button.setText(original_text)
            self.update_channel(updated)
            self.tags_changed.emit(updated)
            if created_catalog_tag:
                self._load_tags()

        def failed(message):
            self._tag_add.setEnabled(True)
            self._tag_remove.setEnabled(True)
            button.setText(original_text)
            QMessageBox.warning(self, "Tags", message)

        run_api(api, change, done, failed, parent=self)

    def _next_count(self):
        if len(self._count_inflight) >= 3 or not self._count_queue or not self.api:
            return
        cid = self._count_queue.pop(0)
        if cid in self._count_inflight:
            self._count_queue.append(cid)
            return
        self._count_inflight.add(cid)
        generation = self._count_generation
        def done(count):
            if generation != self._count_generation:
                return
            self._count_inflight.discard(cid)
            if any(c.id == cid for c in self._all_channels):
                self._on_uploaded_count_loaded(cid, count)
            self._next_count()
        def failed(message):
            if generation != self._count_generation:
                return
            self._count_inflight.discard(cid)
            self._on_uploaded_count_error(cid, message)
            self._next_count()
        run_api(self.api, self.api.get_uploaded_count, done, failed, cid, parent=self)
        self._next_count()

    # ------------------------------------------------------------------
    # AI types (dropdown contents for the AI model selectors)
    # ------------------------------------------------------------------
    def set_ai_types(self, types: list[AITypeOut]) -> None:
        self._ai_types = list(types)
        self._rebuild_ai_model_combos()

    def update_ai_type(self, ai_type: AITypeOut) -> None:
        for index, current in enumerate(self._ai_types):
            if current.api_type == ai_type.api_type:
                if current == ai_type:
                    return
                self._ai_types[index] = ai_type
                break
        else:
            self._ai_types.append(ai_type)
        self._rebuild_ai_model_combos()

    def on_ai_type_deleted(self, api_type: str) -> None:
        for index, current in enumerate(self._ai_types):
            if current.api_type == api_type:
                self._ai_types[index] = AITypeOut(api_type=api_type)
                break
        else:
            return
        self._rebuild_ai_model_combos()

    def set_ai_providers(self, providers: list[AICustomProviderOut]) -> None:
        self._ai_providers = list(providers)
        self._rebuild_ai_model_combos()

    def update_ai_provider(self, provider: AICustomProviderOut) -> None:
        for index, current in enumerate(self._ai_providers):
            if current.id == provider.id:
                if current == provider:
                    return
                self._ai_providers[index] = provider
                break
        else:
            self._ai_providers.append(provider)
        self._rebuild_ai_model_combos()

    def remove_ai_provider(self, provider_id) -> None:
        pid = str(provider_id)
        if not any(str(p.id) == pid for p in self._ai_providers):
            return
        self._ai_providers = [p for p in self._ai_providers if str(p.id) != pid]
        self._rebuild_ai_model_combos()

    def _rebuild_ai_model_combos(self) -> None:
        """Пересобирает оба списка AI-моделей, сохраняя выбранный элемент."""
        combos = []
        if hasattr(self, "_set_ai_model"):
            combos.append(self._set_ai_model)
        if hasattr(self, "_bulk_ai_model"):
            combos.append(self._bulk_ai_model)
        for combo in combos:
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Disabled", None)
            combo.addItem("Auto (first working type/provider)", "auto")
            for ai_type in self._ai_types:
                label = ai_type.api_type if ai_type.is_active else f"{ai_type.api_type} [inactive]"
                combo.addItem(label, ai_type.api_type)
            for provider in self._ai_providers:
                label = provider.name if provider.is_active else f"{provider.name} [inactive]"
                combo.addItem(label, f"custom:{provider.id}")
            combo.blockSignals(False)
            index = combo.findData(current) if current is not None else -1
            combo.setCurrentIndex(index if index >= 0 else 0)
        # Список типов мог подгрузиться после выбора канала — восстанавливаем
        # значение канала/массового выделения в пересобранных комбобоксах.
        if self._current_channel and hasattr(self, "_set_ai_model"):
            self._select_ai_model(self._set_ai_model, self._current_channel.ai_model)
        selected = self._selected_channels() if hasattr(self, "_channel_table") else []
        if len(selected) > 1:
            common = selected[0].ai_model
            if any(c.ai_model != common for c in selected):
                common = None
            self._select_ai_model(self._bulk_ai_model, common)

    @staticmethod
    def _select_ai_model(combo: QComboBox, value: str | None) -> None:
        index = combo.findData(value)
        if index < 0 and value:
            # Значение (например, custom:<id> удалённого провайдера) есть на
            # сервере, но отсутствует в списке — показываем отдельной строкой,
            # чтобы сохранение не затёрло его молча на null.
            combo.addItem(f"{value} (missing)", value)
            index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)


    def set_proxies(self, proxies: list[ProxyOut]) -> None:
        self._proxies = list(proxies)
        self._channel_model.set_proxies(proxies)
        self._refresh_proxy_controls()

    def update_proxy(self, proxy: ProxyOut) -> None:
        for index, current in enumerate(self._proxies):
            if current.id == proxy.id:
                if current == proxy:
                    return
                self._proxies[index] = proxy
                break
        else:
            self._proxies.append(proxy)
        self._channel_model.update_proxy(proxy)

        combo_index = self._set_proxy.findData(str(proxy.id))
        state = "" if proxy.is_active else " [inactive]"
        text = f"{proxy.url}{state}"
        if combo_index >= 0:
            if self._set_proxy.itemText(combo_index) != text:
                self._set_proxy.setItemText(combo_index, text)
        else:
            self._set_proxy.addItem(text, str(proxy.id))

    def remove_proxy(self, proxy_id) -> None:
        self._proxies = [proxy for proxy in self._proxies if str(proxy.id) != str(proxy_id)]
        self._channel_model.remove_proxy(proxy_id)
        combo_index = self._set_proxy.findData(str(proxy_id))
        if combo_index >= 0:
            self._set_proxy.removeItem(combo_index)

    def _set_last_used_proxy(self) -> None:
        settings = getattr(self.window(), "settings", None)
        proxy_id = getattr(settings, "last_proxy_id", None)
        if not proxy_id or not any(str(proxy.id) == proxy_id for proxy in self._proxies):
            QMessageBox.critical(self, "Proxy error", "The last used proxy is deleted or was not saved.")
            return
        self._set_proxy.setCurrentIndex(self._set_proxy.findData(proxy_id))

    def _set_last_storage_state(self) -> None:
        if not self._current_channel:
            return
        manager = getattr(self.window(), "browser_manager", None)
        if manager is None:
            return
        if not manager.has_common_state():
            QMessageBox.critical(
                self,
                "Storage state",
                "The common browser storage state has not been saved yet.\n"
                "Open the common browser, sign in and close it first.",
            )
            return
        if manager.is_profile_active(f"channel_{self._current_channel.id}"):
            QMessageBox.critical(
                self,
                "Storage state",
                "Close the channel browser before replacing its storage state.",
            )
            return
        name = self._current_channel.youtube_title or self._current_channel.title or "Untitled"
        reply = QMessageBox.warning(
            self,
            "Confirm",
            f"Replace the browser storage state (cookies and site data) of channel "
            f"'{name}' with the last saved state from the common browser?\n\n"
            "The channel's current browser login state will be overwritten.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        if manager.apply_common_state(self._current_channel.id):
            QMessageBox.information(
                self,
                "Storage state",
                "Channel storage state replaced with the common browser state.",
            )
        else:
            QMessageBox.critical(self, "Storage state", "Failed to apply the storage state.")

    def _open_channel_browser(self) -> None:
        if not self._current_channel:
            return
        opener = getattr(self.window(), "open_browser", None)
        if opener:
            opener(f"channel_{self._current_channel.id}", self._current_channel.proxy_id)

    def _auto_select_channel_proxy(self) -> None:
        if not self.api or not self._current_channel:
            return
        channel_id = self._current_channel.id
        auto_switch = self._set_proxy_auto.isChecked()
        gen = self._auto_proxy_seq.get(channel_id, 0) + 1
        self._auto_proxy_seq[channel_id] = gen
        self._auto_proxy_pending[channel_id] = gen
        self._auto_proxy_manual.pop(channel_id, None)
        self.set_channel_operation_loading(channel_id, "select_proxy", True)
        run_api(
            self.api,
            self.api.auto_select_proxy,
            lambda proxy, cid=channel_id, sw=auto_switch, g=gen: self._on_auto_proxy_selected(cid, proxy, sw, g),
            lambda message, cid=channel_id, g=gen: self._on_auto_proxy_error(cid, message, g),
            parent=self,
        )

    def _cancel_auto_proxy(self, channel_id) -> None:
        # Отменяет автоподбор и сразу разблокирует кнопку. Запрос в пути будет
        # проигнорирован по номеру поколения, когда вернётся.
        if channel_id not in self._auto_proxy_pending:
            return
        del self._auto_proxy_pending[channel_id]
        self.set_channel_operation_loading(channel_id, "select_proxy", False)

    def _on_auto_proxy_selected(self, channel_id, proxy: ProxyOut, auto_switch: bool, gen: int) -> None:
        if not self.api or self._auto_proxy_pending.get(channel_id) != gen:
            return
        run_api(
            self.api,
            self.api.update_channel_proxy,
            lambda channel, cid=channel_id, g=gen: self._on_channel_proxy_saved(cid, channel, g),
            lambda message, cid=channel_id, g=gen: self._on_auto_proxy_error(cid, message, g),
            channel_id,
            {"proxy_id": str(proxy.id), "auto_switch": auto_switch},
            parent=self,
        )

    def _on_channel_proxy_saved(self, channel_id, channel: ChannelOut, gen: int) -> None:
        if self._auto_proxy_pending.get(channel_id) != gen:
            # Подбор отменили ручным выбором прокси, пока запрос сохранения был
            # в пути. Повторно применяем ручной выбор пользователя на сервере.
            choice = self._auto_proxy_manual.pop(channel_id, None)
            if choice is not None and self.api:
                proxy_id, auto_switch = choice
                run_api(
                    self.api,
                    self.api.update_channel_proxy,
                    lambda updated: self.update_channel(updated),
                    lambda msg: None,
                    channel_id,
                    {"proxy_id": proxy_id, "auto_switch": auto_switch},
                    parent=self,
                )
            return
        del self._auto_proxy_pending[channel_id]
        self._auto_proxy_manual.pop(channel_id, None)
        self.set_channel_operation_loading(channel_id, "select_proxy", False)
        self.update_channel(channel)

    def _on_auto_proxy_error(self, channel_id, message: str, gen: int) -> None:
        if self._auto_proxy_pending.get(channel_id) != gen:
            return
        del self._auto_proxy_pending[channel_id]
        self.set_channel_operation_loading(channel_id, "select_proxy", False)
        QMessageBox.critical(self, "Proxy selection failed", message)

    def update_channel(self, channel: ChannelOut) -> None:
        selected = {c.id for c in self._selected_channels()}
        for existing in self._all_channels:
            if existing.id == channel.id:
                channel.loading_operations = existing.loading_operations.copy()
                break
        with QSignalBlocker(self._channel_table.selectionModel()):
            self._channel_model.update_channel(channel)
            if selected:
                self._restore_channel_selection(selected)
        for index, existing in enumerate(self._all_channels):
            if existing.id == channel.id:
                self._all_channels[index] = channel
                break
        else:
            self._all_channels.append(channel)
            self.refresh_uploaded_count_if_channel(channel.id)
        if self._group_tags.isChecked():
            self._tag_groups.set_channels(self._all_channels)
        if self._current_channel and self._current_channel.id == channel.id:
            self._current_channel = channel
            self._refresh_overview()
            self._refresh_settings()

    def set_channel_operation_loading(self, channel_id, operation: str, loading: bool) -> None:
        for channel in self._all_channels:
            if str(channel.id) != str(channel_id):
                continue
            if loading:
                channel.loading_operations.add(operation)
            else:
                channel.loading_operations.discard(operation)
            self._channel_model.update_channel(channel)
            if self._current_channel and self._current_channel.id == channel.id:
                self._current_channel = channel
                self._refresh_overview()
                self._refresh_action_buttons()
            return

    def set_channel_avatar(self, channel_id: str, pixmap: QPixmap) -> None:
        if self._current_channel and str(self._current_channel.id) == channel_id:
            scaled = pixmap.scaled(
                OVERVIEW_AVATAR_SIZE,
                OVERVIEW_AVATAR_SIZE,
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            self._ov_avatar.setPixmap(scaled)

    def _on_avatar_loaded(self, channel_id, pixmap: QPixmap) -> None:
        self.set_channel_avatar(str(channel_id), pixmap)

    def remove_channel(self, channel_id) -> None:
        self._all_channels = [c for c in self._all_channels if c.id != channel_id]
        self._channel_model.remove_channel(channel_id)
        if self._group_tags.isChecked():
            self._tag_groups.set_channels(self._all_channels)

    def refresh_files_if_channel(self, channel_id) -> None:
        if self._current_channel and str(self._current_channel.id) == str(channel_id):
            self._files_tab._load_files()

    def refresh_tasks_if_channel(self, channel_id) -> None:
        if self._current_channel and str(self._current_channel.id) == str(channel_id):
            self._tasks_tab._load()

    def update_tasks(self, tasks: list[TaskOut]) -> None:
        if self._current_channel:
            cid = str(self._current_channel.id)
            channel_tasks = [t for t in tasks if str(t.channel_id) == cid]
            if channel_tasks:
                self._tasks_tab.update_tasks(channel_tasks)

    def _selected_channels(self) -> list[ChannelOut]:
        if self._group_tags.isChecked():
            return self._tag_groups.selected_channels()
        return self._selected_table_channels()

    def _selected_table_channels(self) -> list[ChannelOut]:
        indexes = self._channel_table.selectionModel().selectedRows()
        rows = sorted({self._proxy_model.mapToSource(index).row() for index in indexes})
        channels = []
        seen = set()
        for row in rows:
            channel = self._channel_model.get_channel(row)
            if channel is not None and channel.id not in seen:
                seen.add(channel.id)
                channels.append(channel)
        return channels

    def _on_selection_changed(self, *_args) -> None:
        selected = self._selected_channels()
        self._refresh_select_all_button()
        if len(selected) > 1:
            # Массовый режим: обычная панель канала скрывается, вместо неё —
            # панель настроек для нескольких каналов.
            self._current_channel = None
            self._tabs.hide()
            self._bulk_widget.show()
            self._refresh_bulk_settings(selected)
            return
        self._bulk_widget.hide()
        self._tabs.show()
        self._set_current_channel(selected[0] if selected else None)

    def _set_current_channel(self, channel: ChannelOut | None) -> None:
        if self._current_channel is not None and channel is not None and self._current_channel.id == channel.id:
            self._current_channel = channel
            return
        self._current_channel = channel
        self._current_uploaded_count = None
        self._uploaded_count_failed = False
        self._folder_pick = _FOLDER_UNSET
        self._refresh_overview()
        self._refresh_settings()
        self._load_upload_limit()
        self._load_uploaded_count()
        if channel:
            self._files_tab.set_channel(channel)
            self._tasks_tab.set_channel(channel)

    def _toggle_select_all(self) -> None:
        if self._group_tags.isChecked():
            if self._tag_groups.all_active_selected():
                self._tag_groups._active.clear_selection()
            else:
                self._tag_groups.select_all_active()
            return
        if self._all_visible_selected():
            self._channel_table.clearSelection()
        else:
            self._channel_table.selectAll()

    def _all_visible_selected(self) -> bool:
        if self._group_tags.isChecked():
            return self._tag_groups.all_active_selected()
        total = self._proxy_model.rowCount()
        if total == 0:
            return False
        selected = len(self._channel_table.selectionModel().selectedRows())
        return selected >= total

    def _refresh_select_all_button(self) -> None:
        self._select_all_btn.setText("Deselect All" if self._all_visible_selected() else "Select All")

    def _refresh_bulk_settings(self, selected: list[ChannelOut]) -> None:
        def common(values: list, default):
            first = values[0]
            return first if all(value == first for value in values) else default

        self._bulk_title.setText(f"Bulk settings — {len(selected)} channels selected")
        self._bulk_max_batch.setValue(max(1, min(200, common([c.max_batch_size for c in selected], 200))))
        self._bulk_min_batch.setValue(max(1, min(200, common([c.min_batch_size for c in selected], 1))))
        self._bulk_inter_delay.set_seconds(max(0, common([c.inter_upload_delay_seconds for c in selected], 2)))
        self._bulk_shuffle.setChecked(common([c.shuffle_upload_order for c in selected], False))
        self._bulk_skip_blocked_videos.setChecked(
            common([c.skip_blocked_videos for c in selected], False)
        )
        self._bulk_ul_enabled.setChecked(common([c.upload_limit_enabled for c in selected], False))
        self._bulk_ul_count.setValue(max(1, common([c.upload_limit_count for c in selected], 1)))
        self._bulk_ul_period.setValue(max(1, common([c.upload_limit_period_hours for c in selected], 24)))
        self._select_ai_model(
            self._bulk_ai_model,
            common([c.ai_model for c in selected], None),
        )
        self._bulk_ai_generate_description.setChecked(common([c.ai_generate_description for c in selected], False))
        self._bulk_ai_use_cached.setChecked(common([c.ai_use_cached for c in selected], False))
        self._bulk_proxy_auto.setChecked(common([c.proxy_auto_switch for c in selected], False))
        self._bulk_video_title_template.setText(common([c.title_template for c in selected], "{filename}"))
        self._bulk_video_desc_template.setPlainText(common([c.description_template for c in selected], ""))
        self._bulk_video_privacy.setCurrentText(common([c.privacy_status for c in selected], "public"))
        self._bulk_video_category.setText(common([c.default_category_id or "" for c in selected], ""))
        # Новый набор выделенных каналов — сбрасываем явный выбор папки.
        self._bulk_folder_pick = _FOLDER_UNSET
        self._refresh_bulk_folder_label(selected)

    def _save_bulk_settings(self) -> None:
        selected = self._selected_channels()
        if len(selected) < 2 or not self.api:
            return
        reply = QMessageBox.question(
            self,
            "Confirm",
            f"Apply these settings to {len(selected)} channels?",
        )
        if reply != QMessageBox.Yes:
            return
        enabled = self._bulk_ul_enabled.isChecked()
        count = self._bulk_ul_count.value()
        period = self._bulk_ul_period.value()
        payload = {
            "channel_ids": [str(channel.id) for channel in selected],
            "max_batch_size": self._bulk_max_batch.value(),
            "min_batch_size": self._bulk_min_batch.value(),
            "inter_upload_delay_seconds": self._bulk_inter_delay.seconds(),
            "shuffle_upload_order": self._bulk_shuffle.isChecked(),
            "skip_blocked_videos": self._bulk_skip_blocked_videos.isChecked(),
            "upload_limit_enabled": enabled,
            "upload_limit_count": count,
            "upload_limit_period_hours": period,
            "ai_model": self._bulk_ai_model.currentData(),
            "ai_generate_description": self._bulk_ai_generate_description.isChecked(),
            "ai_use_cached": self._bulk_ai_use_cached.isChecked(),
            "proxy_auto_switch": self._bulk_proxy_auto.isChecked(),
        }
        self._bulk_save_count = len(selected)
        self._set_bulk_saving(True)
        run_api(
            self.api,
            self.api.bulk_update_channels_settings,
            self._on_bulk_saved,
            self._on_bulk_save_error,
            payload,
            parent=self,
        )

    def _save_bulk_video_settings(self) -> None:
        selected = self._selected_channels()
        if len(selected) < 2 or not self.api:
            return
        reply = QMessageBox.question(
            self,
            "Confirm",
            f"Apply these video settings to {len(selected)} channels?",
        )
        if reply != QMessageBox.Yes:
            return
        payload = {
            "channel_ids": [str(channel.id) for channel in selected],
            "title_template": self._bulk_video_title_template.text(),
            "description_template": self._bulk_video_desc_template.toPlainText(),
            "privacy_status": self._bulk_video_privacy.currentText(),
            "default_category_id": self._bulk_video_category.text(),
        }
        self._set_bulk_saving(True)
        run_api(
            self.api,
            self.api.bulk_update_channels_video_settings,
            self._on_bulk_saved,
            self._on_bulk_save_error,
            payload,
            parent=self,
        )

    def _pick_bulk_folder_channel(self) -> None:
        selected = self._selected_channels()
        if not selected:
            return
        selected_ids = {str(channel.id) for channel in selected}
        others = [
            c
            for c in self._all_channels
            if str(c.id) not in selected_ids and c.status != "deleted"
        ]
        if not others:
            QMessageBox.information(self, "Info", "No other channels available.")
            return
        from src.widgets.add_channel_dialog import ChannelPickerDialog

        dlg = ChannelPickerDialog(others, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            # Явный выбор: id канала-источника (общая папка) или None —
            # вернуть каждому выделенному каналу собственную dedicated folder.
            self._bulk_folder_pick = dlg.selected_channel_id()
            self._refresh_bulk_folder_label(selected)

    def _refresh_bulk_folder_label(self, selected: list[ChannelOut]) -> None:
        pick = self._bulk_folder_pick
        if pick is _FOLDER_UNSET or pick is None:
            self._bulk_folder_label.setText("Dedicated folder for each channel")
            return
        for c in self._all_channels:
            if str(c.id) == str(pick):
                name = c.youtube_title or c.title or "Untitled"
                handle = c.handle or "no handle"
                self._bulk_folder_label.setText(f"{name} ({handle})")
                return
        self._bulk_folder_label.setText(f"Channel {pick}")

    def _save_bulk_folder_settings(self) -> None:
        selected = self._selected_channels()
        if len(selected) < 2 or not self.api:
            return
        if self._bulk_folder_pick is _FOLDER_UNSET:
            QMessageBox.information(
                self,
                "Video folder",
                "Pick a source channel folder first, or select the dedicated-folder\n"
                "option in the dialog to switch every channel back to its own folder.",
            )
            return
        if self._bulk_folder_pick is None:
            message = (
                f"Switch all {len(selected)} selected channels back to their own "
                "dedicated folders?"
            )
        else:
            count = sum(1 for c in selected if str(c.id) == str(self._bulk_folder_pick))
            if count:
                QMessageBox.critical(
                    self,
                    "Video folder",
                    "The source channel cannot be part of the selection:\n"
                    "a channel cannot copy its own folder.",
                )
                return
            message = (
                f"Switch all {len(selected)} selected channels to the folder of "
                f"channel '{self._bulk_folder_label.text()}'?"
            )
        reply = QMessageBox.question(self, "Confirm", message)
        if reply != QMessageBox.Yes:
            return
        payload = {
            "channel_ids": [str(channel.id) for channel in selected],
            # Ключ обязателен: null означает dedicated folder (API.md).
            "folder_from_channel_id": self._bulk_folder_pick,
        }
        self._set_bulk_saving(True)
        run_api(
            self.api,
            self.api.bulk_update_channel_folders,
            self._on_bulk_saved,
            self._on_bulk_save_error,
            payload,
            parent=self,
        )

    def _set_bulk_saving(self, saving: bool) -> None:
        # На время сохранения (с любой вкладки) блокируем возможность менять
        # выделение каналов и все кнопки сохранения.
        self._channel_table.setEnabled(not saving)
        self._select_all_btn.setEnabled(not saving)
        self._bulk_save.setEnabled(not saving)
        self._bulk_save.setText("Saving..." if saving else "Save Settings")
        self._bulk_video_save.setEnabled(not saving)
        self._bulk_video_save.setText("Saving..." if saving else "Save Video Settings")
        self._bulk_folder_save.setEnabled(not saving)
        self._bulk_folder_save.setText("Saving..." if saving else "Save Folder Settings")

    def _on_bulk_saved(self, result: ChannelBulkSettingsResult) -> None:
        self._set_bulk_saving(False)
        for channel in result.updated:
            self.update_channel(channel)
        selected = self._selected_channels()
        if len(selected) > 1:
            self._refresh_bulk_settings(selected)
        missing = len(result.not_found)
        if missing:
            QMessageBox.warning(
                self,
                "Saved",
                f"Settings applied to {len(result.updated)} channels.\n"
                f"{missing} channel(s) were not found (possibly deleted).",
            )
        else:
            QMessageBox.information(self, "Saved", f"Settings applied to {len(result.updated)} channels.")

    def _on_bulk_save_error(self, message: str) -> None:
        self._set_bulk_saving(False)
        QMessageBox.critical(self, "Error", f"Failed to apply settings:\n{message}")

    def _refresh_overview(self) -> None:
        ch = self._current_channel
        self._ov_tags.setText(tag_text(ch) if ch else "")
        self._ov_authorized.setText(f"Authorized: {authorization_text(ch)}" if ch else "")
        if not ch:
            self._refresh_action_buttons()
            self._set_proxy_auto_select.setEnabled(False)
            self._ov_title.setText("<h2 style='margin:0;'>Select a channel</h2>")
            self._ov_handle.setText("")
            self._ov_subscribers.setText("")
            self._ov_views.setText("")
            self._ov_metadata_updated.setText("")
            self._ov_avatar.setPixmap(QPixmap())
            self._ov_paused_until.setVisible(False)
            self._ov_pause_reason.setVisible(False)
            self._ov_uploaded.setText("")
            return
        self._refresh_action_buttons()
        title = ch.youtube_title or ch.title or "Untitled"
        self._ov_title.setText(f"<h2 style='margin:0;'>{title}</h2>")
        self._ov_handle.setText(ch.handle or "")
        status = ch.status
        if ch.proxy_recovery_pending:
            status += " (waiting for a working proxy)"
        self._ov_status.setText(status)
        self._ov_session.setText(ch.session_status)
        self._ov_subscribers.setText(
            f"{ch.subscriber_count:,}" if ch.subscriber_count is not None else "Hidden"
        )
        self._ov_views.setText(f"{ch.view_count:,}" if ch.view_count is not None else "-")
        self._ov_metadata_updated.setText(format_local(ch.metadata_updated_at) or "Never")
        self._ov_folder.setText(ch.folder_path or "")
        self._ov_auto.setText("On" if ch.auto_upload_enabled else "Off")
        if ch.status == "paused" and ch.paused_until:
            self._ov_paused_until.setText(format_local(ch.paused_until))
            self._ov_paused_until.setVisible(True)
        else:
            self._ov_paused_until.setText("")
            self._ov_paused_until.setVisible(False)
        if ch.status == "paused" and ch.pause_reason:
            self._ov_pause_reason.setText(ch.pause_reason)
            self._ov_pause_reason.setVisible(True)
        else:
            self._ov_pause_reason.setText("")
            self._ov_pause_reason.setVisible(False)
        self._refresh_uploaded_count()
        if self._avatar_loader:
            cached = self._avatar_loader.get_cached(ch.id)
            if cached:
                self.set_channel_avatar(str(ch.id), cached)
            else:
                self._ov_avatar.setPixmap(QPixmap())
                self._avatar_loader.load(ch.id)

    def _refresh_settings(self) -> None:
        ch = self._current_channel
        if not ch:
            return
        self._set_title.setText(ch.title or "")
        self._set_title_template.setText(ch.title_template)
        self._set_desc_template.setPlainText(ch.description_template)
        self._set_privacy.setCurrentText(ch.privacy_status)
        self._set_category.setText(ch.default_category_id or "")
        self._set_auto.setChecked(ch.auto_upload_enabled)
        self._set_max_batch.setValue(max(1, min(200, ch.max_batch_size)))
        self._set_min_batch.setValue(max(1, min(200, ch.min_batch_size)))
        self._set_inter_delay.set_seconds(ch.inter_upload_delay_seconds)
        self._set_shuffle_order.setChecked(ch.shuffle_upload_order)
        self._set_skip_blocked_videos.setChecked(ch.skip_blocked_videos)
        self._select_ai_model(self._set_ai_model, ch.ai_model)
        self._set_ai_generate_description.setChecked(ch.ai_generate_description)
        self._set_ai_use_cached.setChecked(ch.ai_use_cached)
        self._refresh_proxy_controls()

        # Пока пользователь не трогал выбор папки, отображаем состояние с
        # сервера. Явный выбор (в т.ч. возврат на dedicated folder) не
        # затираем пришедшим по SSE обновлением канала — иначе несохранённый
        # выбор теряется и не попадает в payload при сохранении.
        if self._folder_pick is _FOLDER_UNSET:
            self._folder_from_channel_id = None
            # Владелец папки — канал, чья dedicated folder (…/<uuid>)
            # совпадает с folder_path канала. Раньше владельцем считался
            # любой канал с той же папкой: после того как A указывал на
            # папку B, страница B начинала показывать "A" — выглядело как
            # взаимная рекурсия, хотя folder_path у B не менялся.
            owner_id = self._folder_owner_id(ch)
            if owner_id and owner_id != str(ch.id):
                self._folder_from_channel_id = owner_id
                for c in self._all_channels:
                    if str(c.id) == owner_id and c.status != "deleted":
                        name = c.youtube_title or c.title or "Untitled"
                        handle = c.handle or "no handle"
                        self._set_folder_label.setText(f"{name} ({handle})")
                        break
                else:
                    self._set_folder_label.setText(f"Channel {owner_id}")
            elif owner_id == str(ch.id):
                self._set_folder_label.setText("Dedicated folder")
            else:
                # Папка вне схемы dedicated (custom путь) — показываем путь.
                self._set_folder_label.setText(ch.folder_path or "Dedicated folder")

    def _folder_owner_id(self, ch) -> str | None:
        """Id канала, чья dedicated folder совпадает с папкой канала.

        Dedicated folder канала — VIDEO_BASE_DIR/<uuid>, поэтому совпадение
        ищется по суффиксу "/<uuid>"; UUID фиксированной длины, коллизий
        суффиксов между разными каналами не бывает.
        """
        fp = (ch.folder_path or "").replace("\\", "/").rstrip("/")
        if not fp:
            return None
        for c in self._all_channels:
            if c.status == "deleted":
                continue
            cid = str(c.id)
            if fp.endswith(f"/{cid}"):
                return cid
        return None

    def _refresh_proxy_controls(self) -> None:
        ch = self._current_channel
        self._set_proxy.clear()
        self._set_proxy.addItem("Direct connection", None)
        for proxy in self._proxies:
            state = "" if proxy.is_active else " [inactive]"
            self._set_proxy.addItem(f"{proxy.url}{state}", str(proxy.id))
        if ch and ch.proxy_id:
            index = self._set_proxy.findData(str(ch.proxy_id))
            if index >= 0:
                self._set_proxy.setCurrentIndex(index)
        self._set_proxy_auto.setChecked(ch.proxy_auto_switch if ch else False)
        loading = ch and "select_proxy" in ch.loading_operations
        self._set_proxy_auto_select.setEnabled(bool(ch) and not loading)

    def _load_upload_limit(self) -> None:
        if not self._current_channel or not self.api:
            self._current_upload_limit = None
            self._refresh_upload_limit_settings()
            return
        channel_id = self._current_channel.id
        run_api(
            self.api,
            self.api.get_upload_limit,
            lambda limit, cid=channel_id: self._on_upload_limit_loaded(cid, limit),
            lambda msg: None,
            channel_id,
            parent=self,
        )

    def _on_upload_limit_loaded(self, channel_id, limit: UploadLimitOut) -> None:
        # Ответ мог прийти уже после переключения на другой канал.
        if not self._current_channel or self._current_channel.id != channel_id:
            return
        self._current_upload_limit = limit
        self._refresh_upload_limit_settings()

    def _load_uploaded_count(self) -> None:
        if not self._current_channel or not self.api:
            self._current_uploaded_count = None
            self._uploaded_count_failed = False
            self._refresh_uploaded_count()
            return
        channel_id = self._current_channel.id
        self._uploaded_count_failed = False
        if channel_id in self._count_queue:
            self._count_queue.remove(channel_id)
        self._count_queue.insert(0, channel_id)
        self._next_count()

    def _on_uploaded_count_loaded(self, channel_id, count: ChannelUploadedCount) -> None:
        self._channel_model.set_uploaded_count(channel_id, count.total_uploaded)
        self.uploaded_count_changed.emit(channel_id, count.total_uploaded)
        # Ответ мог прийти уже после переключения на другой канал.
        if not self._current_channel or self._current_channel.id != channel_id:
            return
        self._current_uploaded_count = count
        self._uploaded_count_failed = False
        self._refresh_uploaded_count()

    def _on_uploaded_count_error(self, channel_id, message: str) -> None:
        if not self._current_channel or self._current_channel.id != channel_id:
            return
        # Молчаливое игнорирование оставляло «Loading...» навсегда.
        print(f"[UploadedCount] failed to load for {channel_id}: {message}")
        self._uploaded_count_failed = True
        self._refresh_uploaded_count()

    def _refresh_uploaded_count(self) -> None:
        ch = self._current_channel
        count = self._current_uploaded_count
        if not ch:
            self._ov_uploaded.setText("")
            return
        if count:
            text = f"{count.total_uploaded}"
            if count.is_limited:
                text += " — <span style='color:#ed6c02;'><b>limit reached</b></span>"
            elif count.remaining is not None:
                text += f" ({count.uploads_in_current_period} in {count.period_hours}h window, {count.remaining} remaining)"
            else:
                text += f" ({count.uploads_in_current_period} in {count.period_hours}h window)"
            if count.reset_at:
                text += f", reset at {format_local(count.reset_at)}"
        elif self._uploaded_count_failed:
            text = "—"
        else:
            text = "Loading..."
        self._ov_uploaded.setText(text)

    def refresh_uploaded_count_if_channel(self, channel_id) -> None:
        cid = next((c.id for c in self._all_channels if str(c.id) == str(channel_id)), None)
        if cid is not None and cid not in self._count_queue:
            self._count_queue.append(cid)
            self._next_count()

    def _refresh_upload_limit_settings(self) -> None:
        ch = self._current_channel
        limit = self._current_upload_limit
        if not ch:
            self._set_ul_enabled.setChecked(False)
            self._set_ul_count.setValue(1)
            self._set_ul_period.setValue(24)
            self._set_ul_status.setText("")
            return
        # Prefer live limit state; fall back to channel snapshot fields
        enabled = limit.enabled if limit else ch.upload_limit_enabled
        count = limit.count if limit else ch.upload_limit_count
        period = limit.period_hours if limit else ch.upload_limit_period_hours
        self._set_ul_enabled.setChecked(enabled)
        self._set_ul_count.setValue(max(1, count) if count else 1)
        self._set_ul_period.setValue(max(1, period) if period else 24)
        if limit:
            status = (
                f"{limit.uploads_in_current_period} uploads in current period, "
                f"{limit.remaining} remaining"
            )
            if limit.reset_at:
                status += f", resets at {format_local(limit.reset_at)}"
            if limit.is_limited:
                status += "\n<b>Limit reached — channel paused</b>"
            self._set_ul_status.setText(status)
        else:
            self._set_ul_status.setText("")

    def _save_upload_limit(self) -> None:
        if not self._current_channel or not self.api:
            return
        channel_id = self._current_channel.id
        self.set_channel_operation_loading(channel_id, "save_upload_limit", True)
        enabled = self._set_ul_enabled.isChecked()
        count = self._set_ul_count.value()
        period = self._set_ul_period.value()
        payload = {
            # Формат лимит-эндпоинта
            "enabled": enabled,
            "count": count,
            "period_hours": period,
            # Префиксные поля канала — на случай, если сервер маппит payload
            # напрямую в модель Channel (ошибка "Channel object has no field enabled").
            "upload_limit_enabled": enabled,
            "upload_limit_count": count,
            "upload_limit_period_hours": period,
        }
        print(f"[UploadLimit] saving via upload-limit endpoint: {payload}")
        run_api(
            self.api,
            self.api.update_upload_limit,
            lambda updated, cid=channel_id: self._on_upload_limit_saved(cid, updated),
            lambda message, cid=channel_id: self._on_upload_limit_error(cid, message),
            channel_id,
            payload,
            parent=self,
        )

    def _on_upload_limit_saved(self, channel_id, updated) -> None:
        if isinstance(updated, ChannelOut):
            self.set_channel_operation_loading(channel_id, "save_upload_limit", False)
        else:
            self.set_channel_operation_loading(channel_id, "save_upload_limit", False)
        if isinstance(updated, UploadLimitOut):
            # Сервер вернул лимит-стейт — обновляем только его, канал не трогаем.
            self._current_upload_limit = updated
            self._refresh_upload_limit_settings()
            print("[UploadLimit] saved (UploadLimitOut)")
        else:
            print(f"[UploadLimit] saved, channel status={updated.status} reason={updated.pause_reason}")
            self.update_channel(updated)
        # Refresh live limit state and uploaded count from dedicated endpoints
        self._load_upload_limit()
        self._load_uploaded_count()
        QMessageBox.information(self, "Saved", "Upload limit updated.")

    def _on_upload_limit_error(self, channel_id, message: str) -> None:
        self.set_channel_operation_loading(channel_id, "save_upload_limit", False)
        print(f"[UploadLimit] error: {message}")
        QMessageBox.critical(self, "Error", message)

    def _pick_folder_channel(self) -> None:
        if not self._current_channel:
            return
        others = [
            c
            for c in self._all_channels
            if c.id != self._current_channel.id and c.status != "deleted"
        ]
        if not others:
            QMessageBox.information(self, "Info", "No other channels available.")
            return
        from src.widgets.add_channel_dialog import ChannelPickerDialog

        dlg = ChannelPickerDialog(others, self)
        if dlg.exec() == dlg.DialogCode.Accepted:
            cid = dlg.selected_channel_id()
            # Явный выбор пользователя: id канала (общая папка) или None
            # (вернуть каналу собственную dedicated folder).
            self._folder_pick = cid
            if cid:
                self._folder_from_channel_id = cid
                for ch in others:
                    if str(ch.id) == cid:
                        name = ch.youtube_title or ch.title or "Untitled"
                        handle = ch.handle or "no handle"
                        self._set_folder_label.setText(f"{name} ({handle})")
                        return
                self._set_folder_label.setText(f"Channel {cid}")
            else:
                self._folder_from_channel_id = None
                self._set_folder_label.setText("Dedicated folder")

    def _refresh_action_buttons(self) -> None:
        ch = self._current_channel
        operations = ch.loading_operations if ch else set()
        for button, operation, text in (
            (self._btn_unpause, "unpause", "Unpause"),
            (self._btn_clear_error, "clear_error", "Clear Error"),
            (self._btn_reauth, "reauth", "Re-authorize"),
            (self._btn_delete, "delete", "Delete Channel"),
        ):
            loading = operation in operations
            button.setEnabled(ch is not None and not loading)
            button.setText("Loading..." if loading else text)
        settings_loading = "save_settings" in operations
        self._set_save.setEnabled(ch is not None and not settings_loading)
        self._set_save.setText("Saving..." if settings_loading else "Save Settings")
        limit_loading = "save_upload_limit" in operations
        self._set_ul_save.setEnabled(ch is not None and not limit_loading)
        self._set_ul_save.setText("Saving..." if limit_loading else "Save Upload Limit")
        proxy_loading = "select_proxy" in operations
        self._set_proxy_auto_select.setEnabled(ch is not None and not proxy_loading)
        self._set_proxy_auto_select.setText(
            "Selecting proxy..." if proxy_loading else "Auto-select working proxy"
        )

    def _save_settings(self) -> None:
        if not self._current_channel or not self.api:
            return
        payload = {
            "title": self._set_title.text() or None,
            "title_template": self._set_title_template.text(),
            "description_template": self._set_desc_template.toPlainText(),
            "privacy_status": self._set_privacy.currentText(),
            "default_category_id": self._set_category.text(),
            "auto_upload_enabled": self._set_auto.isChecked(),
            "max_batch_size": self._set_max_batch.value(),
            "min_batch_size": self._set_min_batch.value(),
            "inter_upload_delay_seconds": self._set_inter_delay.seconds(),
            "shuffle_upload_order": self._set_shuffle_order.isChecked(),
            "skip_blocked_videos": self._set_skip_blocked_videos.isChecked(),
            "ai_model": self._set_ai_model.currentData(),
            "ai_generate_description": self._set_ai_generate_description.isChecked(),
            "ai_use_cached": self._set_ai_use_cached.isChecked(),
            "proxy_id": self._set_proxy.currentData(),
            "proxy_auto_switch": self._set_proxy_auto.isChecked(),
        }
        if self._folder_pick is not _FOLDER_UNSET:
            # Явный выбор папки: id канала (общая папка) или null — сервер
            # вернёт каналу собственную dedicated folder.
            payload["folder_from_channel_id"] = self._folder_pick
        channel_id = self._current_channel.id
        if channel_id in self._auto_proxy_pending:
            # Если во время автоподбора пользователь вручную поменял и сохранил
            # прокси (или режим без прокси) — отменяем подбор и сразу
            # разблокируем кнопку; результат поиска будет проигнорирован.
            saved_proxy = str(self._current_channel.proxy_id) if self._current_channel.proxy_id else None
            chosen_proxy = self._set_proxy.currentData() or None
            if chosen_proxy != saved_proxy:
                self._auto_proxy_manual[channel_id] = (chosen_proxy, self._set_proxy_auto.isChecked())
                self._cancel_auto_proxy(channel_id)
        self.set_channel_operation_loading(channel_id, "save_settings", True)
        run_api(
            self.api,
            self.api.update_channel,
            lambda updated, cid=channel_id: self._on_settings_saved(cid, updated),
            lambda message, cid=channel_id: self._on_settings_error(cid, message),
            channel_id,
            payload,
            parent=self,
        )

    def _on_settings_saved(self, channel_id, updated: ChannelOut) -> None:
        self.set_channel_operation_loading(channel_id, "save_settings", False)
        # Выбор папки применён — далее отображаем состояние с сервера.
        folder_sent = self._folder_pick
        self._folder_pick = _FOLDER_UNSET
        # Диагностика смены папки: если сервер вернул старый folder_path,
        # это видно сразу (клиент отображает только то, что прислал сервер).
        if folder_sent is not _FOLDER_UNSET:
            old = self._current_channel.folder_path if self._current_channel else "?"
            print(
                f"[Folder] channel={channel_id} sent folder_from_channel_id="
                f"{folder_sent!r} -> server folder_path: {old!r} => {updated.folder_path!r}"
            )
        self.update_channel(updated)
        if self._current_channel and str(self._current_channel.id) == str(channel_id):
            # Папка могла измениться — перезагружаем файлы и живые счётчики.
            self._files_tab.set_channel(updated)
            self._load_uploaded_count()
            self._load_upload_limit()
        QMessageBox.information(self, "Saved", "Channel settings updated.")

    def _on_settings_error(self, channel_id, message: str) -> None:
        self.set_channel_operation_loading(channel_id, "save_settings", False)
        QMessageBox.critical(self, "Error", message)

    def _unpause(self) -> None:
        if not self._current_channel or not self.api:
            return
        channel_id = self._current_channel.id
        self.set_channel_operation_loading(channel_id, "unpause", True)
        run_api(
            self.api,
            self.api.unpause_channel,
            lambda updated: self._on_action_done(updated, channel_id, "unpause"),
            lambda msg: self._on_action_error(msg, channel_id, "unpause"),
            channel_id,
            parent=self,
        )

    def _clear_error(self) -> None:
        if not self._current_channel or not self.api:
            return
        channel_id = self._current_channel.id
        self.set_channel_operation_loading(channel_id, "clear_error", True)
        run_api(
            self.api,
            self.api.clear_channel_error,
            lambda updated: self._on_action_done(updated, channel_id, "clear_error"),
            lambda msg: self._on_action_error(msg, channel_id, "clear_error"),
            channel_id,
            parent=self,
        )

    def _on_action_done(self, updated: ChannelOut, channel_id, operation: str) -> None:
        self.set_channel_operation_loading(channel_id, operation, False)
        self.update_channel(updated)

    def _on_action_error(self, message: str, channel_id, operation: str) -> None:
        self.set_channel_operation_loading(channel_id, operation, False)
        QMessageBox.critical(self, "Error", message)

    def _reauth(self) -> None:
        if self._current_channel:
            self.set_channel_operation_loading(self._current_channel.id, "reauth", True)
            self.channel_auth_requested.emit(self._current_channel)

    def _delete_channel(self) -> None:
        if not self._current_channel or not self.api:
            return
        reply = QMessageBox.question(
            self,
            "Confirm",
            f"Permanently delete channel '{self._current_channel.youtube_title or self._current_channel.title or 'Untitled'}'?",
        )
        if reply == QMessageBox.Yes:
            channel_id = self._current_channel.id
            self.set_channel_operation_loading(channel_id, "delete", True)
            run_api(
                self.api,
                self.api.delete_channel,
                lambda result, cid=channel_id: self._on_delete_done(cid, result),
                lambda message, cid=channel_id: self._on_delete_error(cid, message),
                channel_id,
                parent=self,
            )

    def _on_delete_done(self, channel_id, _) -> None:
        self.channel_deleted.emit(channel_id)
        self.remove_channel(channel_id)
        self._all_channels = [channel for channel in self._all_channels if channel.id != channel_id]
        if self._current_channel and self._current_channel.id == channel_id:
            self._current_channel = None
            self._refresh_overview()

    def _on_delete_error(self, channel_id, message: str) -> None:
        self.set_channel_operation_loading(channel_id, "delete", False)
        QMessageBox.critical(self, "Error", message)
