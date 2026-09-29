from __future__ import annotations

import json
import socket
from typing import Any

import httpx
from PySide6.QtCore import QThread, Signal

from src.api_client import ApiClient


class SseWorker(QThread):
    """Thread that listens to SSE and emits signals."""

    connection_opened = Signal(str, int)  # subscriber_id, pending_count
    event_received = Signal(str, dict)  # event_name, payload
    disconnected = Signal(str)  # reason
    reconnected = Signal()  # соединение восстановлено после обрыва

    def __init__(self, base_url: str, api_token: str, subscriber_id: str | None = None) -> None:
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.subscriber_id = subscriber_id
        self._running = True
        self._ever_connected = False  # был ли хотя бы один удачный handshake
        self._client: httpx.Client | None = None
        self._network_stream: Any = None

    def stop(self) -> None:
        self._running = False
        # Закрытие httpx-клиента из другого потока не прерывает заблокированный
        # recv() сокета, поэтому дополнительно делаем shutdown — он будит
        # потоковое чтение, и поток завершается быстро.
        stream = self._network_stream
        if stream is not None:
            try:
                sock = stream.get_extra_info("socket")
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
            except Exception:
                pass
        # The worker closes its HTTP client in _listen's finally block.
        # Never wait for a network thread on the GUI thread.

    def run(self) -> None:
        while self._running:
            try:
                connected = self._listen()
            except Exception as exc:
                print(f"[SSE] Listen exception: {exc}")
                connected = False
                if self._running:
                    self.disconnected.emit(str(exc))
            if self._running:
                # Между попытками показываем, что мы переподключаемся.
                self.disconnected.emit("")
                # Спим короткими кусками, чтобы stop() мог быстро завершить поток.
                for _ in range(30):
                    if not self._running:
                        break
                    self.msleep(100)

    def _listen(self) -> bool:
        """Слушает поток. Возвращает True, если handshake connection.opened получен."""
        params: dict[str, Any] = {}
        if self.subscriber_id:
            params["subscriber_id"] = self.subscriber_id

        # Fast connect timeout so a wrong URL does not hang forever;
        # read timeout is disabled because SSE is a long-lived stream.
        timeout = httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0)
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "text/event-stream",
        }

        got_opened = False
        pending_acks: list[str] = []
        self._client = httpx.Client(timeout=timeout)
        try:
            url = f"{self.base_url}/updates/sse"
            with self._client.stream("GET", url, headers=headers, params=params) as response:
                self._network_stream = response.extensions.get("network_stream")
                if not self._running:
                    return False
                print(f"[SSE] Connected, status={response.status_code}")
                response.raise_for_status()

                event_name = "message"
                event_id: str | None = None
                event_data_parts: list[str] = []
                pending_acks: list[str] = []

                for line in response.iter_lines():
                    if not self._running:
                        break
                    if line.startswith("id:"):
                        event_id = line[3:].strip()
                    elif line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        event_data_parts.append(line[5:].strip())
                    elif line == "":
                        # Empty line = end of event
                        if event_data_parts:
                            data_str = "\n".join(event_data_parts)
                            try:
                                payload = json.loads(data_str)
                            except json.JSONDecodeError:
                                payload = {"raw": data_str}

                            if event_name == "connection.opened":
                                if not got_opened:
                                    got_opened = True
                                    if self._ever_connected:
                                        # Переподключение после обрыва — уведомляем UI.
                                        self.reconnected.emit()
                                    self._ever_connected = True
                                sid = payload.get("subscriber_id", "")
                                pending = payload.get("pending_count", 0)
                                self.subscriber_id = sid
                                self.connection_opened.emit(sid, pending)

                            self.event_received.emit(event_name, payload)
                            if event_id:
                                pending_acks.append(event_id)
                                if len(pending_acks) >= 10:
                                    self._ack(pending_acks)
                                    pending_acks.clear()

                        # Reset for next event
                        event_name = "message"
                        event_id = None
                        event_data_parts = []
        finally:
            self._network_stream = None
            client = self._client
            self._client = None
            # При завершении потока не тратим время на ack — события будут
            # доставлены повторно при следующем подключении.
            if pending_acks and self._running:
                self._ack(pending_acks)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        return got_opened

    def _ack(self, event_ids: list[str]) -> None:
        if not self.subscriber_id:
            return
        try:
            ack_client = ApiClient(self.base_url, self.api_token, timeout=10.0)
            ack_client.ack_events(self.subscriber_id, event_ids)
            ack_client.close()
            print(f"[SSE] Acked {len(event_ids)} events")
        except Exception as exc:
            print(f"[SSE] Ack failed: {exc}")
