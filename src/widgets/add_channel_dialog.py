from __future__ import annotations

from uuid import UUID

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from src.models import ChannelOut, ProjectOut, ProxyOut


class ChannelPickerDialog(QDialog):
    """Dialog to pick an existing channel (for folder_from_channel_id).

    OK without a selection (or the dedicated-folder button) means "no source
    channel" — for the caller this resolves to a dedicated folder.
    """

    def __init__(
        self,
        channels: list[ChannelOut],
        parent=None,
        dedicated_label: str = "Use dedicated folder",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Existing Channel")
        self.setMinimumWidth(450)
        self.setMinimumHeight(350)

        layout = QVBoxLayout(self)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search by title or handle...")
        self._search.setClearButtonEnabled(True)
        layout.addWidget(self._search)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SingleSelection)
        for ch in channels:
            name = ch.youtube_title or ch.title or "Untitled"
            handle = ch.handle or "no handle"
            text = f"{name}  ({handle})"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, str(ch.id))
            self._list.addItem(item)
        layout.addWidget(self._list)

        dedicated_row = QHBoxLayout()
        self._dedicated_btn = QPushButton(dedicated_label)
        self._dedicated_btn.clicked.connect(self._choose_dedicated)
        dedicated_row.addWidget(self._dedicated_btn)
        dedicated_row.addStretch()
        layout.addLayout(dedicated_row)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._search.textChanged.connect(self._filter)
        self._list.itemDoubleClicked.connect(self.accept)

    def _choose_dedicated(self) -> None:
        # Neither selected nor current, so selected_channel_id() -> None.
        self._list.clearSelection()
        self._list.setCurrentItem(None)
        self.accept()

    def _filter(self, text: str) -> None:
        lowered = text.lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(lowered not in item.text().lower())

    def selected_channel_id(self) -> str | None:
        item = self._list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None


class AddChannelDialog(QDialog):
    def __init__(
        self,
        projects: list[ProjectOut],
        channels: list[ChannelOut],
        proxies: list[ProxyOut] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add New Channel")
        self.setMinimumWidth(400)

        self._channels = channels
        self._folder_from_channel_id: str | None = None
        self._proxies = proxies or []
        self._closed = False
        self.finished.connect(self._mark_closed)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._project = QComboBox()
        self._project.addItem("Auto select (default)", None)
        for p in projects:
            self._project.addItem(p.name, p.id)
        form.addRow("Project:", self._project)

        self._proxy = QComboBox()
        self._proxy.addItem("Direct connection", None)
        for proxy in self._proxies:
            state = "" if proxy.is_active else " [inactive]"
            self._proxy.addItem(f"{proxy.url}{state}", str(proxy.id))
        self._proxy_auto = QCheckBox("Switch automatically after a proxy failure")
        self._proxy_auto_select = QPushButton("Auto-select working proxy")
        self._proxy_auto_select.clicked.connect(self._auto_select_proxy)
        self._proxy_last = QPushButton("Set last used proxy")
        self._proxy_last.clicked.connect(self._set_last_proxy)
        self._browser = QPushButton("Open Browser")
        self._browser.clicked.connect(self._open_browser)
        # Поколение запроса автоподбора в работе (None — поиска нет).
        self._auto_seq = 0
        self._auto_pending: int | None = None
        self._proxy.currentIndexChanged.connect(self._on_proxy_manually_changed)
        form.addRow("Proxy:", self._proxy)
        form.addRow("Proxy failover:", self._proxy_auto)
        form.addRow(self._proxy_auto_select)
        form.addRow(self._proxy_last, self._browser)

        self._folder_label = QLabel("New folder will be created by server")
        self._folder_btn = QPushButton("Select existing channel...")
        self._folder_btn.clicked.connect(self._pick_folder_channel)
        if not channels:
            self._folder_btn.setEnabled(False)
            self._folder_label.setText("No existing channels")

        folder_row = QHBoxLayout()
        folder_row.addWidget(self._folder_label, 1)
        folder_row.addWidget(self._folder_btn)
        form.addRow("Copy folder from:", folder_row)

        self._use_storage_state = QCheckBox("Sign in with saved common browser session")
        self._use_storage_state.setToolTip(
            "Open the authorization window with cookies from the common browser,\n"
            "so Google recognizes the account and no manual login is needed."
        )
        form.addRow(self._use_storage_state)

        layout.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok_button = btns.button(QDialogButtonBox.Ok)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _on_proxy_manually_changed(self, _index: int) -> None:
        # Ручной выбор прокси (из списка или последнего использованного)
        # отменяет идущий автоподбор и разблокирует кнопки.
        if self._auto_pending is None:
            return
        self._auto_pending = None
        self._ok_button.setEnabled(True)
        self._proxy_auto_select.setEnabled(True)

    def _auto_select_proxy(self) -> None:
        api = getattr(self.parent(), "api", None)
        if api is None:
            return
        self._auto_seq += 1
        self._auto_pending = self._auto_seq
        self._ok_button.setEnabled(False)
        self._proxy_auto_select.setEnabled(False)
        from src.api_worker import run_api
        owner = self.parentWidget() or self
        gen = self._auto_seq
        run_api(
            api,
            api.auto_select_proxy,
            lambda proxy, g=gen: self._proxy_selected(proxy, g),
            lambda message, g=gen: self._proxy_select_error(message, g),
            parent=owner,
        )

    def _proxy_selected(self, proxy: ProxyOut, gen: int) -> None:
        if self._is_closed() or self._auto_pending != gen:
            return
        self._auto_pending = None
        index = self._proxy.findData(str(proxy.id))
        if index < 0:
            self._proxy.addItem(proxy.url, str(proxy.id))
            index = self._proxy.count() - 1
        self._proxy.setCurrentIndex(index)
        self._ok_button.setEnabled(True)
        self._proxy_auto_select.setEnabled(True)

    def _proxy_select_error(self, message: str, gen: int) -> None:
        if self._is_closed() or self._auto_pending != gen:
            return
        self._auto_pending = None
        self._ok_button.setEnabled(True)
        self._proxy_auto_select.setEnabled(True)
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(self, "Proxy selection failed", message)

    def _accept(self) -> None:
        if self._use_storage_state.isChecked():
            manager = getattr(self.parent(), "browser_manager", None)
            if manager is None or not manager.has_common_state():
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.critical(
                    self,
                    "Storage state",
                    "The common browser storage state has not been saved yet.\n"
                    "Open the common browser, sign in and close it first,\n"
                    "or uncheck the option.",
                )
                return
        self._closed = True
        self.accept()

    def reject(self) -> None:
        self._closed = True
        super().reject()

    def _mark_closed(self, _result: int) -> None:
        self._closed = True

    def _is_closed(self) -> bool:
        try:
            import shiboken6
            return self._closed or not shiboken6.isValid(self)
        except ImportError:
            return self._closed

    def _set_last_proxy(self) -> None:
        settings = getattr(self.parent(), "settings", None)
        proxy_id = getattr(settings, "last_proxy_id", None)
        if not proxy_id or not any(str(proxy.id) == proxy_id for proxy in self._proxies):
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Proxy error", "The last used proxy is deleted or was not saved.")
            return
        self._proxy.setCurrentIndex(self._proxy.findData(proxy_id))

    def _open_browser(self) -> None:
        owner = self.parent()
        opener = getattr(owner, "open_browser", None)
        if not opener:
            return
        proxy_id = self._proxy.currentData()
        opener("new_channel", proxy_id)

    def _pick_folder_channel(self) -> None:
        dlg = ChannelPickerDialog(
            self._channels, self, dedicated_label="Create new dedicated folder"
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            cid = dlg.selected_channel_id()
            if cid:
                self._folder_from_channel_id = cid
                # Find channel name for label
                for ch in self._channels:
                    if str(ch.id) == cid:
                        name = ch.youtube_title or ch.title or "Untitled"
                        handle = ch.handle or "no handle"
                        self._folder_label.setText(f"{name} ({handle})")
                        return
                self._folder_label.setText(f"Channel {cid}")
            else:
                self._folder_from_channel_id = None
                self._folder_label.setText("New folder will be created by server")

    def use_storage_state(self) -> bool:
        return self._use_storage_state.isChecked()

    def payload(self) -> dict:
        project_id = self._project.currentData()
        return {
            "project_id": str(project_id) if project_id is not None else None,
            "folder_from_channel_id": self._folder_from_channel_id,
            "proxy_id": self._proxy.currentData(),
            "proxy_auto_switch": self._proxy_auto.isChecked(),
        }
