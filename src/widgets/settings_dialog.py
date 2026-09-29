import httpx
from PySide6.QtCore import QThread
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QMessageBox,
)

from src.settings import AppSettings


class SseTestThread(QThread):
    """Проверяет, что SSE endpoint реально отдаёт connection.opened."""

    def __init__(self, url: str, token: str) -> None:
        super().__init__()
        self.url = url.rstrip("/")
        self.token = token
        self.ok = False
        self.error = ""

    def run(self) -> None:
        try:
            timeout = httpx.Timeout(connect=8.0, read=8.0, write=8.0, pool=8.0)
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Accept": "text/event-stream",
            }
            with httpx.Client(timeout=timeout) as client:
                with client.stream("GET", f"{self.url}/updates/sse", headers=headers) as resp:
                    resp.raise_for_status()
                    # Достаточно первых строк, чтобы увидеть событие connection.opened.
                    for line in resp.iter_lines():
                        if "connection.opened" in line or line.startswith("data:"):
                            self.ok = True
                            return
                    self.error = "SSE stream opened but no connection.opened received"
        except Exception as exc:  # noqa: BLE001
            self.error = str(exc)


class SettingsDialog(QDialog):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Connection Settings")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.url_edit = QLineEdit(self.settings.server_url)
        self.token_edit = QLineEdit(self.settings.api_token)
        self.token_edit.setEchoMode(QLineEdit.Password)

        form.addRow("Server URL:", self.url_edit)
        form.addRow("API Token:", self.token_edit)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        self.test_btn = QPushButton("Test Connection")
        self.save_btn = QPushButton("Save")
        self.cancel_btn = QPushButton("Cancel")
        self.save_btn.setDefault(True)

        buttons.addWidget(self.test_btn)
        buttons.addStretch()
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.cancel_btn)
        layout.addLayout(buttons)

        self.test_btn.clicked.connect(self._test_connection)
        self.save_btn.clicked.connect(self._save)
        self.cancel_btn.clicked.connect(self.reject)

    def _test_connection(self) -> None:
        url = self.url_edit.text().strip()
        token = self.token_edit.text().strip()

        self.test_btn.setEnabled(False)
        self.test_btn.setText("Testing...")

        def on_finished():
            self.test_btn.setEnabled(True)
            self.test_btn.setText("Test Connection")
            if thread.ok:
                QMessageBox.information(self, "Success", "Connection successful (SSE handshake OK)!")
            else:
                QMessageBox.critical(self, "Error", f"Connection failed:\n{thread.error}")

        thread = SseTestThread(url, token)
        thread.finished.connect(on_finished)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _save(self) -> None:
        self.settings.server_url = self.url_edit.text().strip()
        self.settings.api_token = self.token_edit.text().strip()
        self.settings.save()
        self.accept()
