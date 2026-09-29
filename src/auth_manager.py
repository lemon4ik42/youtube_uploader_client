from __future__ import annotations

import sys
import time
import urllib.parse
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal


class AuthManager(QObject):
    """Google OAuth in Microsoft Edge WebView2 via pywebview."""

    code_received = Signal(str, str)
    error_occurred = Signal(str)
    authorization_timed_out = Signal()
    authorization_closed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None
        self._buffer = b""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_timeout)
        self._handled_states: set[str] = set()
        self._finished = False

    def stop(self) -> None:
        self._timer.stop()
        process = self._process
        self._process = None
        if process:
            process.terminate()
            if not process.waitForFinished(1500):
                process.kill()
            process.deleteLater()

    def open_browser(
        self,
        url: str,
        proxy_settings: dict | None = None,
        timeout_seconds: float | None = None,
        user_data_dir: str | None = None,
    ) -> None:
        self.stop()
        self._buffer = b""
        self._finished = False
        proxy = (proxy_settings or {}).get("proxy") or {}
        proxy_url = proxy.get("url") if isinstance(proxy, dict) else None

        environment = QProcessEnvironment.systemEnvironment()
        args = environment.value("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS")
        if proxy_url:
            args = f"{args} --proxy-server={proxy_url}".strip()
        environment.insert("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", args)

        redirect_uri = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get(
            "redirect_uri", [""]
        )[0]
        module = Path(__file__).with_name("auth_webview2.py")
        process = QProcess(self)
        process.setProcessEnvironment(environment)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._read_output)
        process.finished.connect(self._process_finished)
        args = [str(module), url, redirect_uri]
        if user_data_dir:
            args.append(str(user_data_dir))
        process.start(sys.executable, args)
        if not process.waitForStarted(5000):
            self.error_occurred.emit(
                "Cannot start Microsoft Edge WebView2. Install Microsoft Edge WebView2 Runtime."
            )
            return
        self._process = process
        self._started_at = time.monotonic()
        seconds = timeout_seconds if timeout_seconds and timeout_seconds > 0 else 600
        self._timer.start(int(seconds * 1000))

    def _read_output(self) -> None:
        if not self._process:
            return
        self._buffer += bytes(self._process.readAllStandardOutput())
        while b"\n" in self._buffer:
            raw, self._buffer = self._buffer.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("OAUTH_READY:"):
                # Webview-процесс дошёл до показа окна — логируем тайминг.
                started = getattr(self, "_started_at", None)
                if started is not None:
                    print(f"[Auth] webview window shown in {time.monotonic() - started:.1f}s")
                continue
            if not line.startswith("OAUTH_REDIRECT:"):
                if line.startswith("OAUTH_ERROR:"):
                    self.error_occurred.emit(line[len("OAUTH_ERROR:"):].strip())
                continue
            query = urllib.parse.parse_qs(urllib.parse.urlparse(line[15:]).query)
            error = query.get("error", [None])[0]
            code = query.get("code", [None])[0]
            state = query.get("state", [None])[0]
            if error:
                self.error_occurred.emit(f"Authorization failed: {error}")
            elif code and state and state not in self._handled_states:
                self._handled_states.add(state)
                self._finished = True
                self.code_received.emit(code, state)
                self.stop()

    def _on_timeout(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.stop()
        self.authorization_timed_out.emit()

    def _process_finished(self) -> None:
        if self._process is not None:
            self._process = None
        self._timer.stop()
        if not self._finished:
            self._finished = True
            self.authorization_closed.emit()
