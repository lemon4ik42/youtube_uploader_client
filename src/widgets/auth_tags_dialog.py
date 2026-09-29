from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)

from src.api_worker import run_api


class AuthTagsDialog(QDialog):
    """Choose callback tags without modifying a channel before OAuth succeeds."""

    def __init__(self, api, channel=None, parent=None):
        super().__init__(parent)
        self.api = api
        self._closed = False
        self._creating = False
        self._loading = False
        self._tags = {tag.id: tag for tag in channel.tags} if channel else {}
        self._checked = set(self._tags)
        self.finished.connect(self._mark_closed)
        self.setWindowTitle("Complete channel authorization")
        self.resize(520, 440)
        layout = QVBoxLayout(self)
        hint = QLabel("Choose tags, then finish authorization. You can also create a new tag here.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._replace = QCheckBox("Replace channel tags with the checked tags")
        self._replace.setToolTip("Unchecked: preserve existing tags. Checked with no tags selected: clear all tags.")
        layout.addWidget(self._replace)
        self._summary = QLabel()
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search tags...")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter)
        layout.addWidget(self._search)
        self._list = QListWidget()
        self._list.itemChanged.connect(self._selection_changed)
        layout.addWidget(self._list)
        row = QHBoxLayout()
        self._name = QLineEdit()
        self._name.setMaxLength(100)
        self._name.setPlaceholderText("New tag name")
        self._create = QPushButton("Create and select")
        self._create.setAutoDefault(False)
        self._create.clicked.connect(self._create_tag)
        row.addWidget(self._name, 1)
        row.addWidget(self._create)
        layout.addLayout(row)
        status_row = QHBoxLayout()
        self._status = QLabel()
        self._status.setTextFormat(Qt.PlainText)
        self._status.setWordWrap(True)
        self._retry = QPushButton("Refresh tags")
        self._retry.setAutoDefault(False)
        self._retry.clicked.connect(self._load)
        status_row.addWidget(self._status, 1)
        status_row.addWidget(self._retry)
        layout.addLayout(status_row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._finish = buttons.button(QDialogButtonBox.Ok)
        self._finish.setText("Finish authorization")
        buttons.button(QDialogButtonBox.Cancel).setText("Cancel authorization")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._replace.toggled.connect(self._update_summary)
        self._render()
        self._update_summary()
        self._load()

    def _mark_closed(self, _result):
        self._closed = True

    def _load(self):
        if self._loading or self._creating:
            return
        self._loading = True
        self._retry.setEnabled(False)
        self._create.setEnabled(False)
        self._status.setText("Loading tags...")
        run_api(self.api, self.api.get_tags, self._loaded, self._load_failed,
                parent=self.parentWidget() or self)

    def _loaded(self, tags):
        if self._closed:
            return
        self._loading = False
        self._retry.setEnabled(True)
        self._create.setEnabled(True)
        self._tags.update({tag.id: tag for tag in tags})
        self._render()
        self._status.setText("")

    def _load_failed(self, message):
        if self._closed:
            return
        self._loading = False
        self._retry.setEnabled(True)
        self._create.setEnabled(True)
        self._status.setText(f"Could not load tags: {message}. Retry or finish without changing tags.")

    def _render(self):
        self._list.blockSignals(True)
        self._list.clear()
        for tag in sorted(self._tags.values(), key=lambda t: (t.name.casefold(), t.name)):
            item = QListWidgetItem(tag.name)
            item.setData(Qt.UserRole, tag.id)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if tag.id in self._checked else Qt.Unchecked)
            self._list.addItem(item)
        self._list.blockSignals(False)
        self._filter(self._search.text())

    def _filter(self, text):
        for row in range(self._list.count()):
            item = self._list.item(row)
            item.setHidden(text.casefold() not in item.text().casefold())

    def _selection_changed(self, item):
        tag_id = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self._checked.add(tag_id)
        else:
            self._checked.discard(tag_id)
        self._replace.setChecked(True)
        self._update_summary()

    def _update_summary(self):
        if not self._replace.isChecked():
            text = "Existing tags will be preserved. New channels will have no tags."
        elif self._checked:
            text = f"Selected tags: {len(self._checked)} (including tags hidden by search)."
        else:
            text = "No tags selected: all existing channel tags will be cleared."
        self._summary.setText(text)

    def _create_tag(self):
        name = self._name.text().strip()
        if not name or self._creating or self._loading:
            return
        self._creating = True
        self._finish.setEnabled(False)
        self._create.setEnabled(False)
        self._retry.setEnabled(False)
        self._status.setText("Creating tag...")
        run_api(self.api, self.api.create_tag, self._created, self._create_failed,
                name, parent=self.parentWidget() or self)

    def _creation_finished(self):
        self._creating = False
        self._finish.setEnabled(True)
        self._create.setEnabled(True)
        self._retry.setEnabled(True)

    def _created(self, tag):
        if self._closed:
            return
        self._creation_finished()
        self._tags[tag.id] = tag
        self._checked.add(tag.id)
        self._replace.setChecked(True)
        self._name.clear()
        self._search.clear()
        self._render()
        self._update_summary()
        self._status.setText("")

    def _create_failed(self, message):
        if self._closed:
            return
        self._creation_finished()
        self._status.setText(f"Could not create tag: {message}")

    def accept(self):
        if not self._creating:
            super().accept()

    def tag_ids(self):
        return sorted(self._checked, key=str) if self._replace.isChecked() else None
