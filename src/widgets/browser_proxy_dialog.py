from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QMessageBox, QPushButton

from src.api_worker import run_api
from src.models import ProxyOut


class BrowserProxyDialog(QDialog):
    def __init__(self, proxies: list[ProxyOut], settings, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open Browser")
        self._settings = settings
        self._proxies = proxies
        self._closed = False
        self.finished.connect(self._mark_closed)
        self._proxy = QComboBox()
        self._proxy.addItem("Direct connection", None)
        for proxy in proxies:
            self._proxy.addItem(proxy.url, str(proxy.id))
        self._auto = QPushButton("Auto-select working proxy")
        self._last = QPushButton("Set last used proxy")
        self._ok = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok.accepted.connect(self._accept)
        self._ok.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.addRow("Proxy:", self._proxy)
        form.addRow(self._auto)
        form.addRow(self._last)
        form.addRow(self._ok)
        self._auto.clicked.connect(self._auto_select)
        self._last.clicked.connect(self._set_last)
        # Поколение запроса автоподбора в работе (None — поиска нет).
        self._auto_seq = 0
        self._auto_pending: int | None = None
        self._proxy.currentIndexChanged.connect(self._on_proxy_manually_changed)
        last_url = getattr(settings, "last_proxy_url", None)
        self._last_label = QLabel(f"Last used proxy: {last_url or 'none'}")
        self._last_label.setWordWrap(True)
        form.addRow(self._last_label)

    def proxy_id(self) -> str | None:
        return self._proxy.currentData()

    def _on_proxy_manually_changed(self, _index: int) -> None:
        # Ручной выбор прокси отменяет идущий автоподбор и разблокирует
        # кнопки, включая OK.
        if self._auto_pending is None:
            return
        self._auto_pending = None
        self._set_busy(False)

    def _auto_select(self) -> None:
        api = getattr(self.parent(), "api", None)
        if not api:
            return
        self._auto_seq += 1
        self._auto_pending = self._auto_seq
        self._set_busy(True)
        # The dialog may be closed while the request is running. Keep the
        # worker owned by the main window so closing this dialog cannot destroy
        # a live QThread.
        owner = self.parentWidget() or self
        gen = self._auto_seq
        run_api(
            api,
            api.auto_select_proxy,
            lambda proxy, g=gen: self._selected(proxy, g),
            lambda message, g=gen: self._error(message, g),
            parent=owner,
        )

    def _selected(self, proxy: ProxyOut, gen: int) -> None:
        if self._is_closed() or self._auto_pending != gen:
            return
        self._auto_pending = None
        index = self._proxy.findData(str(proxy.id))
        if index < 0:
            self._proxy.addItem(proxy.url, str(proxy.id))
            index = self._proxy.count() - 1
        self._proxy.setCurrentIndex(index)
        self._set_busy(False)

    def _set_last(self) -> None:
        proxy_id = getattr(self._settings, "last_proxy_id", None)
        if not proxy_id or self._proxy.findData(proxy_id) < 0:
            QMessageBox.critical(self, "Proxy error", "The last used proxy is deleted or was not saved.")
            return
        self._proxy.setCurrentIndex(self._proxy.findData(proxy_id))

    def _error(self, message: str, gen: int) -> None:
        if self._is_closed() or self._auto_pending != gen:
            return
        self._auto_pending = None
        self._set_busy(False)
        QMessageBox.critical(self, "Proxy selection failed", message)

    def _set_busy(self, busy: bool) -> None:
        if self._is_closed():
            return
        self._ok.button(QDialogButtonBox.Ok).setEnabled(not busy)
        self._auto.setEnabled(not busy)

    def _accept(self) -> None:
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
