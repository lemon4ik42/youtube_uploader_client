from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.api_client import ApiClient
from src.api_worker import run_api
from src.models import ProxyCheckResult, ProxyOut


class ProxyDialog(QDialog):
    def __init__(self, proxy: ProxyOut | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Proxy" if proxy else "Add Proxy")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._url = QLineEdit(proxy.url if proxy else "")
        self._url.setPlaceholderText("socks5://user:password@example.com:1080")
        self._active = QCheckBox("Active")
        self._active.setChecked(proxy.is_active if proxy else True)
        form.addRow("URL:", self._url)
        form.addRow(self._active)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_validated(self) -> None:
        parsed = urlparse(self._url.text().strip())
        if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or not parsed.port:
            QMessageBox.warning(self, "Invalid Proxy", "Use http://, https://, or socks5:// with host and port.")
            return
        self.accept()

    def payload(self) -> dict:
        return {
            "url": self._url.text().strip(),
            "is_active": self._active.isChecked(),
        }


class ProxiesPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.api: ApiClient | None = None
        self._proxies: list[ProxyOut] = []
        layout = QVBoxLayout(self)
        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAcceptDrops(True)
        self._table.installEventFilter(self)
        self._table.setToolTip("Drop a UTF-8 text file here to import one proxy URL per line")
        layout.addWidget(self._table)
        self._model = QStandardItemModel(0, 6)
        self._model.setHorizontalHeaderLabels(
            ["Name", "URL", "Active", "Channels", "Last Check", "Working"]
        )
        self._table.setModel(self._model)
        self._table.horizontalHeader().setStretchLastSection(True)

        import_row = QHBoxLayout()
        self._import_label = QLabel("Importing proxies...")
        self._import_progress = QProgressBar()
        self._import_progress.setRange(0, 0)
        self._import_progress.setTextVisible(False)
        import_row.addWidget(self._import_label)
        import_row.addWidget(self._import_progress, 1)
        self._import_row = QWidget()
        self._import_row.setLayout(import_row)
        self._import_row.hide()
        layout.addWidget(self._import_row)

        self._importing = False

        buttons = QHBoxLayout()
        self._add = QPushButton("Add Proxy")
        self._edit = QPushButton("Edit Proxy")
        self._delete = QPushButton("Delete Proxy")
        self._check = QPushButton("Check All")
        self._check_one = QPushButton("Check Selected")
        self._refresh = QPushButton("Refresh")
        for button in (self._add, self._edit, self._delete, self._check, self._check_one):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(self._refresh)
        layout.addLayout(buttons)
        self._add.clicked.connect(self._add_proxy)
        self._edit.clicked.connect(self._edit_proxy)
        self._delete.clicked.connect(self._delete_proxy)
        self._check.clicked.connect(self._check_all)
        self._check_one.clicked.connect(self._check_selected)
        self._refresh.clicked.connect(self.refresh)

    def set_api(self, api: ApiClient) -> None:
        self.api = api

    def set_proxies(self, proxies: list[ProxyOut]) -> None:
        self._proxies = list(proxies)
        self._model.removeRows(0, self._model.rowCount())
        for proxy in proxies:
            self._model.appendRow(self._proxy_row(proxy))

    def update_proxy(self, proxy: ProxyOut) -> None:
        for row, current in enumerate(self._proxies):
            if current.id != proxy.id:
                continue
            if current == proxy:
                return
            self._proxies[row] = proxy
            values = self._proxy_values(proxy)
            for column, value in enumerate(values):
                item = self._model.item(row, column)
                if item.text() != value:
                    item.setText(value)
            self._set_working_color(self._model.item(row, len(values) - 1), proxy)
            return
        self._proxies.append(proxy)
        self._model.appendRow(self._proxy_row(proxy))

    def update_proxies(self, proxies: list[ProxyOut]) -> None:
        for proxy in proxies:
            self.update_proxy(proxy)

    def remove_proxy(self, proxy_id) -> None:
        for row, proxy in enumerate(self._proxies):
            if str(proxy.id) == str(proxy_id):
                del self._proxies[row]
                self._model.removeRow(row)
                return

    @staticmethod
    def _proxy_values(proxy: ProxyOut) -> list[str]:
        return [
            str(proxy.id),
            proxy.url,
            "Yes" if proxy.is_active else "No",
            str(proxy.assigned_channels),
            proxy.last_checked_at.astimezone().strftime("%Y-%m-%d %H:%M") if proxy.last_checked_at else "",
            "Yes" if proxy.last_check_ok is True else "No" if proxy.last_check_ok is False else "",
        ]

    def _proxy_row(self, proxy: ProxyOut) -> list[QStandardItem]:
        row = [QStandardItem(value) for value in self._proxy_values(proxy)]
        self._set_working_color(row[-1], proxy)
        for item in row:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return row

    @staticmethod
    def _set_working_color(item: QStandardItem, proxy: ProxyOut) -> None:
        if proxy.last_check_ok is True:
            item.setForeground(QColor("#2e7d32"))
        elif proxy.last_check_ok is False:
            item.setForeground(QColor("#c62828"))
        else:
            item.setData(None, Qt.ForegroundRole)

    def refresh(self) -> None:
        if self.api:
            run_api(self.api, self.api.get_proxies, self.set_proxies, self._error, parent=self)

    def _selected(self) -> ProxyOut | None:
        rows = self._table.selectionModel().selectedRows()
        return self._proxies[rows[0].row()] if rows else None

    def _add_proxy(self) -> None:
        if not self.api:
            return
        dialog = ProxyDialog(parent=self)
        if dialog.exec():
            payload = dialog.payload()
            run_api(self.api, self.api.create_proxy, lambda _: self.refresh(), self._error, payload, parent=self)

    def _edit_proxy(self) -> None:
        proxy = self._selected()
        if not self.api or not proxy:
            return
        dialog = ProxyDialog(proxy, self)
        if dialog.exec():
            run_api(self.api, self.api.update_proxy, lambda _: self.refresh(), self._error, proxy.id, dialog.payload(), parent=self)

    def _delete_proxy(self) -> None:
        proxy = self._selected()
        if not self.api or not proxy:
            return
        if QMessageBox.question(self, "Confirm", f"Delete proxy '{proxy.id}'?") == QMessageBox.Yes:
            run_api(self.api, self.api.delete_proxy, lambda _: self.refresh(), self._error, proxy.id, parent=self)

    def _check_all(self) -> None:
        if self.api:
            self._set_checking(True)
            run_api(self.api, self.api.check_proxies, self._on_checked_all, self._on_check_error, parent=self)

    def _check_selected(self) -> None:
        proxy = self._selected()
        if self.api and proxy:
            self._set_checking(True)
            run_api(self.api, self.api.check_proxy, self._on_checked_one, self._on_check_error, proxy.id, parent=self)

    def _set_checking(self, checking: bool) -> None:
        self._check.setEnabled(not checking)
        self._check_one.setEnabled(not checking)
        self._check.setText("Checking..." if checking else "Check All")

    def _on_checked_all(self, result: ProxyCheckResult) -> None:
        self._set_checking(False)
        self.update_proxies(result.proxies)
        # Ответ может содержать сокращённые объекты (только id/url/status) —
        # перезагружаем полный список с сервера, чтобы не потерять колонки.
        self.refresh()
        QMessageBox.information(self, "Proxy check", f"Checked: {result.checked}, working: {result.working}, failed: {result.failed}")

    def _on_checked_one(self, proxy: ProxyOut) -> None:
        self._set_checking(False)
        self.update_proxy(proxy)
        QMessageBox.information(self, "Proxy check", f"{proxy.id}: {proxy.status}")

    def _on_check_error(self, message: str) -> None:
        self._set_checking(False)
        self._error(message)

    def _error(self, message: str) -> None:
        QMessageBox.critical(self, "Proxy Error", message)

    def dragEnterEvent(self, event) -> None:
        if self._importing:
            event.ignore()
            return
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._table and event.type() == event.Type.DragEnter:
            self.dragEnterEvent(event)
            return True
        if watched is self._table and event.type() == event.Type.Drop:
            self.dropEvent(event)
            return True
        return super().eventFilter(watched, event)

    def dropEvent(self, event) -> None:
        if self._importing:
            event.ignore()
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if len(paths) != 1:
            QMessageBox.warning(self, "Invalid proxy file", "Drop exactly one text file.")
            return
        self._import_proxy_file(paths[0])
        event.acceptProposedAction()

    def _set_importing(self, importing: bool) -> None:
        self._importing = importing
        self.setAcceptDrops(not importing)
        self._table.setAcceptDrops(not importing)
        self._import_row.setVisible(importing)

    def _import_proxy_file(self, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8-sig") as source:
                lines = [line.strip() for line in source if line.strip()]
        except OSError as exc:
            QMessageBox.critical(self, "Proxy import failed", str(exc))
            return
        if not lines:
            QMessageBox.warning(self, "Invalid proxy file", "The file contains no proxy URLs.")
            return
        parsed = [urlparse(line) for line in lines]
        invalid = [line for line, item in zip(lines, parsed) if item.scheme not in {"http", "https", "socks5"} or not item.hostname or not item.port]
        if invalid:
            QMessageBox.critical(self, "Invalid proxy file", "Invalid proxy URL:\n" + invalid[0])
            return
        existing = {proxy.url.rstrip("/") for proxy in self._proxies}
        unique = []
        ignored = 0
        for line in lines:
            normalized = line.rstrip("/")
            if normalized in existing or normalized in {item.rstrip("/") for item in unique}:
                ignored += 1
            else:
                unique.append(line)
        if not self.api:
            return
        if not unique:
            QMessageBox.information(self, "Proxy import", f"Added: 0, ignored existing: {ignored}")
            return
        if len(unique) == 1:
            parsed_proxy = urlparse(unique[0])
            payload = {"url": unique[0]}
            self._set_importing(True)
            run_api(
                self.api,
                self.api.create_proxy,
                lambda _: self._on_single_imported(ignored),
                self._import_error,
                payload,
                parent=self,
            )
            return
        payload = []
        for index, proxy_url in enumerate(unique, start=1):
            parsed_proxy = urlparse(proxy_url)
            payload.append({"url": proxy_url})
        self._set_importing(True)
        run_api(
            self.api,
            self.api.import_proxies,
            lambda result: self._on_imported(result, ignored),
            self._import_error,
            payload,
            parent=self,
        )

    def _on_single_imported(self, local_duplicates: int) -> None:
        self._set_importing(False)
        self.refresh()
        QMessageBox.information(self, "Proxy import", f"Added: 1, ignored existing: {local_duplicates}")

    def _on_imported(self, result, local_duplicates: int) -> None:
        self._set_importing(False)
        self.refresh()
        QMessageBox.information(self, "Proxy import", f"Added: {result.added}, ignored existing: {result.duplicates + local_duplicates}")

    def _import_error(self, message: str) -> None:
        self._set_importing(False)
        QMessageBox.critical(self, "Proxy import failed", message)
