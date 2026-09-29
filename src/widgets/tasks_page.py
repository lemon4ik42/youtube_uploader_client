from PySide6.QtCore import QSortFilterProxyModel, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.api_client import ApiClient
from src.api_worker import run_api
from src.models import ChannelOut, TaskOut
from src.widgets.task_table_model import TaskTableModel


class TasksPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.api: ApiClient | None = None
        self._channels: list[ChannelOut] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Filters
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Channel:"))
        self._channel_filter = QComboBox()
        self._channel_filter.addItem("All", None)
        filter_layout.addWidget(self._channel_filter)

        filter_layout.addWidget(QLabel("Status:"))
        self._status_filter = QComboBox()
        self._status_filter.addItems([
            "All", "pending", "session_created", "uploading", "retry", "completed", "failed", "cancelled"
        ])
        filter_layout.addWidget(self._status_filter)

        self._active_only = QCheckBox("Active only")
        filter_layout.addWidget(self._active_only)

        filter_layout.addWidget(QLabel("Search file:"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by file name...")
        self._search.setClearButtonEnabled(True)
        filter_layout.addWidget(self._search)

        self._refresh_btn = QPushButton("Refresh")
        filter_layout.addWidget(self._refresh_btn)
        filter_layout.addStretch()
        layout.addLayout(filter_layout)

        # Table
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)

        self._model = TaskTableModel(max_tasks=200)
        self._proxy = QSortFilterProxyModel()
        self._proxy.setSourceModel(self._model)
        self._proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._proxy.setFilterKeyColumn(0)  # File column
        self._table.setModel(self._proxy)
        self._table.setSortingEnabled(True)
        layout.addWidget(self._table)

        # Connections
        self._refresh_btn.clicked.connect(self.refresh)
        self._channel_filter.currentIndexChanged.connect(self.refresh)
        self._status_filter.currentIndexChanged.connect(self.refresh)
        self._active_only.stateChanged.connect(self.refresh)
        self._search.textChanged.connect(self._proxy.setFilterFixedString)

        self._all_tasks: list[TaskOut] = []

    def set_api(self, api: ApiClient) -> None:
        self.api = api

    def set_channels(self, channels: list[ChannelOut]) -> None:
        self._channels = channels
        current = self._channel_filter.currentData()
        # Блокируем сигналы комбобокса: clear/addItem/setCurrentIndex дёргают
        # currentIndexChanged -> refresh(), и каждый обновленный снапшот
        # каналов запускал до трёх полных перезагрузок задач.
        self._channel_filter.blockSignals(True)
        self._channel_filter.clear()
        self._channel_filter.addItem("All", None)
        for ch in channels:
            self._channel_filter.addItem(ch.youtube_title or ch.title or "Untitled", ch.id)
        # restore selection if possible
        for i in range(self._channel_filter.count()):
            if self._channel_filter.itemData(i) == current:
                self._channel_filter.setCurrentIndex(i)
                break
        else:
            self._channel_filter.setCurrentIndex(0)
        self._channel_filter.blockSignals(False)

    def set_tasks(self, tasks: list[TaskOut]) -> None:
        self._all_tasks = tasks
        self._model.set_tasks(tasks)

    def refresh(self) -> None:
        if not self.api:
            return
        channel_id = self._channel_filter.currentData()
        active_only = self._active_only.isChecked()
        status = None
        stext = self._status_filter.currentText()
        if stext != "All":
            status = [stext]

        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("Refreshing...")
        run_api(
            self.api,
            self.api.get_tasks,
            self._on_tasks_loaded,
            self._on_tasks_error,
            channel_id=channel_id,
            active=active_only if active_only else None,
            status=status,
            limit=200,
            parent=self,
        )

    def _on_tasks_loaded(self, tasks: list[TaskOut]) -> None:
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("Refresh")
        self._all_tasks = tasks
        self._model.set_tasks(tasks)

    def _on_tasks_error(self, message: str) -> None:
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("Refresh")
        # Silently ignore on background refresh; show only on explicit user action
        # We can't easily distinguish here, so just don't popup to avoid spam.

    def _apply_text_filter(self) -> None:
        self._proxy.setFilterFixedString(self._search.text())

    def update_tasks(self, tasks: list[TaskOut]) -> None:
        self._model.update_tasks(tasks)
        self._proxy.invalidate()

    def update_task(self, task: TaskOut) -> None:
        self._model.update_task(task)
        self._proxy.invalidate()

    def remove_task(self, task_id) -> None:
        self._model.remove_task(task_id)
        self._proxy.invalidate()

    def add_tasks(self, tasks: list[TaskOut]) -> None:
        for t in tasks:
            self._model.update_task(t)
        self._proxy.invalidate()
