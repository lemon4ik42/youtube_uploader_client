from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from src.models import (
    AckResponse,
    AICustomProviderOut,
    AITypeOut,
    AiPromptsOut,
    AuthCallbackPayload,
    AuthStartResponse,
    ChannelAvatarOut,
    ChannelBulkSettingsResult,
    ChannelOut,
    ChannelTagOut,
    ChannelSessionStatusOut,
    ChannelUploadedCount,
    ConnectionOpened,
    DeleteFileResponse,
    FolderVideoOut,
    ProjectOut,
    ProxyCheckResult,
    ProxyImportResult,
    ProxyOut,
    TaskOut,
    UploadFileResult,
    UploadLimitOut,
)

# Таймаут для загрузки файлов: nginx хостинга разрывает долгие соединения,
# поэтому даём запас в несколько минут на одно видео.
UPLOAD_TIMEOUT = 600.0

# Автоподбор рабочего прокси: сервер проверяет все активные прокси через
# YouTube Data API, при большом списке это занимает минуты. Соединение может
# обрываться обратным прокси (например, nginx proxy_read_timeout) без ответа —
# "Server disconnected without sending a response". Запрос безопасен для
# повтора: побочных эффектов нет, проверки коалесцируются на сервере, и
# каждая следующая попытка дожидается уже идущую массовую проверку.
AUTO_SELECT_ATTEMPTS = 3
AUTO_SELECT_RETRY_DELAY = 2.0
AUTO_SELECT_TIMEOUT = 600.0

# Установка TCP-соединения: неверный адрес сервера должен давать ошибку быстро,
# независимо от того, сколько разрешено ждать ответа на сам запрос.
CONNECT_TIMEOUT = 15.0


class ApiError(Exception):
    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class _ChunkedFile:
    """Обёртка над файловым объектом для chunked multipart upload.

    httpx определяет длину файла через ``fileno()``/``seek()``/``tell()``.
    Убираем эти методы у обёртки, чтобы httpx не мог вычислить
    ``Content-Length`` и использовал ``Transfer-Encoding: chunked``.
    Файл читается частями (по 64 КБ внутри httpx), не загружаясь
    целиком в память.
    """

    def __init__(self, path: str) -> None:
        self._file = open(path, "rb")

    def read(self, size: int = -1) -> bytes:
        return self._file.read(size)

    def close(self) -> None:
        self._file.close()


class ApiClient:
    def __init__(self, base_url: str, api_token: str, timeout: float = 300.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        # Лимиты соединений: небольшой пул, чтобы параллельные загрузки
        # не забивали память и сеть сотней одновременных коннектов.
        limits = httpx.Limits(max_connections=10, max_keepalive_connections=10)
        # Подключение ограничиваем отдельно: неверный адрес должен отваливаться
        # быстро, а вот ответа от живого сервера можно ждать долго.
        self._timeout = httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT))
        self._client = httpx.Client(timeout=self._timeout, limits=limits)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_token}"}

    def _request(self, method: str, path: str, timeout: float | None = None, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = self._client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.TimeoutException as exc:
            # ReadTimeout/ConnectTimeout не наследуются от NetworkError, поэтому
            # ловим их отдельно — иначе исключение утекает наружу «сырым»
            # ("The read operation timed out") и не попадает в логику ретраев.
            raise ApiError(f"Timeout: the server did not respond in time ({exc.__class__.__name__})") from exc
        except httpx.TransportError as exc:
            raise ApiError(f"Network error: {exc or exc.__class__.__name__}") from exc

        if response.status_code >= 400:
            try:
                body = response.json()
                detail = body.get("detail", response.text)
                if not isinstance(detail, str):
                    detail = response.text
            except Exception:
                detail = response.text
            print(f"[API] {method} {url} -> {response.status_code}: {detail}")
            raise ApiError(detail, response.status_code)

        if response.status_code == 204:
            return None
        if not response.content:
            return None
        return response.json()

    # ------------------------------------------------------------------
    # Projects
    # ------------------------------------------------------------------
    def get_projects(self) -> list[ProjectOut]:
        data = self._request("GET", "/projects")
        return [ProjectOut.model_validate(p) for p in data]

    def create_project(self, payload: dict[str, Any]) -> ProjectOut:
        data = self._request("POST", "/projects", json=payload)
        return ProjectOut.model_validate(data)

    def update_project(self, project_id: UUID, payload: dict[str, Any]) -> ProjectOut:
        data = self._request("PUT", f"/projects/{project_id}", json=payload)
        return ProjectOut.model_validate(data)

    def delete_project(self, project_id: UUID) -> None:
        self._request("DELETE", f"/projects/{project_id}")

    # ------------------------------------------------------------------
    # Proxies
    # ------------------------------------------------------------------
    def get_proxies(self) -> list[ProxyOut]:
        data = self._request("GET", "/proxies")
        return [ProxyOut.model_validate(proxy) for proxy in data]

    def create_proxy(self, payload: dict[str, Any]) -> ProxyOut:
        data = self._request("POST", "/proxies", json=payload)
        return ProxyOut.model_validate(data)

    def import_proxies(self, proxies: list[dict[str, str]]) -> ProxyImportResult:
        data = self._request("POST", "/proxies/import", json={"proxies": proxies})
        return ProxyImportResult.model_validate(data)

    def update_proxy(self, proxy_id: UUID, payload: dict[str, Any]) -> ProxyOut:
        data = self._request("PUT", f"/proxies/{proxy_id}", json=payload)
        return ProxyOut.model_validate(data)

    def delete_proxy(self, proxy_id: UUID) -> None:
        self._request("DELETE", f"/proxies/{proxy_id}")

    def auto_select_proxy(self) -> ProxyOut:
        attempt = 0
        while True:
            attempt += 1
            try:
                data = self._request("POST", "/proxies/auto-select", timeout=AUTO_SELECT_TIMEOUT)
                return ProxyOut.model_validate(data)
            except ApiError as exc:
                # status_code == 0 — сетевая ошибка без ответа сервера (обрыв
                # соединения, таймаут). HTTP-ошибки сервера не ретраим.
                if exc.status_code != 0 or attempt >= AUTO_SELECT_ATTEMPTS:
                    raise
                print(f"[API] auto-select attempt {attempt} failed: {exc.message}; retrying")
                time.sleep(AUTO_SELECT_RETRY_DELAY * attempt)

    def check_proxies(self) -> ProxyCheckResult:
        data = self._request("POST", "/proxies/check")
        return ProxyCheckResult.model_validate(data)

    def check_proxy(self, proxy_id: UUID) -> ProxyOut:
        data = self._request("POST", f"/proxies/{proxy_id}/check")
        return ProxyOut.model_validate(data)

    # ------------------------------------------------------------------
    # AI types / prompts
    # ------------------------------------------------------------------
    def get_ai_types(self) -> list[AITypeOut]:
        data = self._request("GET", "/ai-types")
        return [AITypeOut.model_validate(item) for item in data]

    def get_ai_type(self, api_type: str) -> AITypeOut:
        data = self._request("GET", f"/ai-types/{api_type}")
        return AITypeOut.model_validate(data)

    def update_ai_type(self, api_type: str, payload: dict[str, Any]) -> AITypeOut:
        data = self._request("PUT", f"/ai-types/{api_type}", json=payload)
        return AITypeOut.model_validate(data)

    def delete_ai_type(self, api_type: str) -> None:
        self._request("DELETE", f"/ai-types/{api_type}")

    def get_ai_providers(self) -> list[AICustomProviderOut]:
        data = self._request("GET", "/ai-providers")
        return [AICustomProviderOut.model_validate(item) for item in data]

    def create_ai_provider(self, payload: dict[str, Any]) -> AICustomProviderOut:
        data = self._request("POST", "/ai-providers", json=payload)
        return AICustomProviderOut.model_validate(data)

    def update_ai_provider(self, provider_id: UUID, payload: dict[str, Any]) -> AICustomProviderOut:
        data = self._request("PUT", f"/ai-providers/{provider_id}", json=payload)
        return AICustomProviderOut.model_validate(data)

    def delete_ai_provider(self, provider_id: UUID) -> None:
        self._request("DELETE", f"/ai-providers/{provider_id}")

    def get_ai_prompts(self) -> AiPromptsOut:
        data = self._request("GET", "/ai-prompts")
        return AiPromptsOut.model_validate(data)

    def update_ai_prompts(self, payload: dict[str, Any]) -> AiPromptsOut:
        data = self._request("PUT", "/ai-prompts", json=payload)
        return AiPromptsOut.model_validate(data)

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------
    def get_tags(self) -> list[ChannelTagOut]:
        return [ChannelTagOut.model_validate(tag) for tag in self._request("GET", "/tags")]

    def create_tag(self, name: str) -> ChannelTagOut:
        return ChannelTagOut.model_validate(self._request("POST", "/tags", json={"name": name}))

    def assign_channel_tag(self, channel_id: UUID, tag_id: UUID) -> ChannelOut:
        return ChannelOut.model_validate(self._request("PUT", f"/channels/{channel_id}/tags/{tag_id}"))

    def remove_channel_tag(self, channel_id: UUID, tag_id: UUID) -> ChannelOut:
        return ChannelOut.model_validate(self._request("DELETE", f"/channels/{channel_id}/tags/{tag_id}"))

    def get_channels(self) -> list[ChannelOut]:
        data = self._request("GET", "/channels")
        return [ChannelOut.model_validate(c) for c in data]

    def get_channel(self, channel_id: UUID) -> ChannelOut:
        data = self._request("GET", f"/channels/{channel_id}")
        return ChannelOut.model_validate(data)

    def get_channel_session_status(self, channel_id: UUID) -> ChannelSessionStatusOut:
        data = self._request("GET", f"/channels/{channel_id}/session-status")
        return ChannelSessionStatusOut.model_validate(data)

    def get_channel_avatar(self, channel_id: UUID) -> ChannelAvatarOut:
        data = self._request("GET", f"/channels/{channel_id}/avatar")
        return ChannelAvatarOut.model_validate(data)

    def get_folder_videos(self, channel_id: UUID) -> list[FolderVideoOut]:
        data = self._request("GET", f"/channels/{channel_id}/folder-videos")
        return [FolderVideoOut.model_validate(v) for v in data]

    def update_channel(self, channel_id: UUID, payload: dict[str, Any]) -> ChannelOut:
        data = self._request("PUT", f"/channels/{channel_id}", json=payload)
        return ChannelOut.model_validate(data)

    def update_channel_proxy(self, channel_id: UUID, payload: dict[str, Any]) -> ChannelOut:
        data = self._request("PUT", f"/channels/{channel_id}/proxy", json=payload)
        return ChannelOut.model_validate(data)

    def get_upload_limit(self, channel_id: UUID) -> UploadLimitOut:
        data = self._request("GET", f"/channels/{channel_id}/upload-limit")
        return UploadLimitOut.model_validate(data)

    def update_upload_limit(self, channel_id: UUID, payload: dict[str, Any]) -> ChannelOut | UploadLimitOut:
        data = self._request("PUT", f"/channels/{channel_id}/upload-limit", json=payload)
        # Сервер может вернуть как ChannelOut, так и UploadLimitOut — поддерживаем оба.
        if isinstance(data, dict) and "enabled" in data:
            return UploadLimitOut.model_validate(data)
        return ChannelOut.model_validate(data)

    def bulk_update_channels_settings(self, payload: dict[str, Any]) -> ChannelBulkSettingsResult:
        data = self._request("PUT", "/channels/bulk-settings", json=payload)
        return ChannelBulkSettingsResult.model_validate(data)

    def bulk_update_channels_video_settings(self, payload: dict[str, Any]) -> ChannelBulkSettingsResult:
        data = self._request("PUT", "/channels/bulk-video-settings", json=payload)
        return ChannelBulkSettingsResult.model_validate(data)

    def bulk_update_channel_folders(self, payload: dict[str, Any]) -> ChannelBulkSettingsResult:
        data = self._request("PUT", "/channels/bulk-folder-settings", json=payload)
        return ChannelBulkSettingsResult.model_validate(data)

    def get_uploaded_count(self, channel_id: UUID) -> ChannelUploadedCount:
        data = self._request("GET", f"/channels/{channel_id}/uploaded-count")
        return ChannelUploadedCount.model_validate(data)

    def get_uploaded_videos_count(self, channel_id: UUID) -> dict[str, Any]:
        data = self._request("GET", f"/channels/{channel_id}/videos/count")
        return data

    def unpause_channel(self, channel_id: UUID) -> ChannelOut:
        data = self._request("POST", f"/channels/{channel_id}/unpause")
        return ChannelOut.model_validate(data)

    def clear_channel_error(self, channel_id: UUID) -> ChannelOut:
        data = self._request("POST", f"/channels/{channel_id}/clear-error")
        return ChannelOut.model_validate(data)

    def delete_channel(self, channel_id: UUID) -> None:
        self._request("DELETE", f"/channels/{channel_id}")

    def start_auth(self, payload: dict[str, Any]) -> AuthStartResponse:
        data = self._request("POST", "/channels/auth/start", json=payload)
        return AuthStartResponse.model_validate(data)

    def callback_auth(self, payload: AuthCallbackPayload) -> ChannelOut:
        data = self._request("POST", "/channels/auth/callback", json=payload.model_dump(mode="json", exclude_none=True))
        return ChannelOut.model_validate(data)

    # ------------------------------------------------------------------
    # Uploads
    # ------------------------------------------------------------------
    def upload_file(
        self,
        channel_id: UUID,
        file_path: str,
        title: str | None = None,
        description: str | None = None,
    ) -> UploadFileResult:
        """Загружает один файл на отдельном соединении chunked-загрузкой.

        Для параллельной загрузки каждый вызов создаёт собственный
        ``httpx.Client`` — иначе все потоки делят один клиент/connection pool
        синхронного httpx и загрузка фактически сериализуется.
        """
        file_wrapper = _ChunkedFile(file_path)
        file_name = Path(file_path).name
        file_size = Path(file_path).stat().st_size
        size_mb = file_size / (1024 * 1024)
        files: dict[str, Any] = {"file": (file_name, file_wrapper, "application/octet-stream")}
        data: dict[str, str] = {}
        if title:
            data["title"] = title
        if description:
            data["description"] = description
        url = f"{self.base_url}/uploads/{channel_id}/upload"
        start = time.monotonic()
        try:
            with httpx.Client(timeout=UPLOAD_TIMEOUT) as client:
                request = client.build_request(
                    "POST", url, headers=self._headers(), data=data, files=files
                )
                print(
                    f"[Upload] {file_name} ({size_mb:.1f} MB) "
                    f"headers: {dict(request.headers)}"
                )
                response = client.send(request)
                elapsed = time.monotonic() - start
                print(
                    f"[Upload] {file_name} done in {elapsed:.1f}s, "
                    f"status {response.status_code}"
                )
        except httpx.NetworkError as exc:
            elapsed = time.monotonic() - start
            print(f"[Upload] {file_name} network error after {elapsed:.1f}s: {exc}")
            raise ApiError(f"Network error: {exc}") from exc
        finally:
            file_wrapper.close()
        if response.status_code >= 400:
            try:
                body = response.json()
                detail = body.get("detail", response.text)
                if not isinstance(detail, str):
                    detail = response.text
            except Exception:
                detail = response.text
            raise ApiError(detail, response.status_code)
        return UploadFileResult.model_validate(response.json())

    def delete_file(self, channel_id: UUID, file_name: str) -> DeleteFileResponse:
        from urllib.parse import quote
        data = self._request("DELETE", f"/uploads/{channel_id}/files/{quote(file_name, safe='')}" )
        return DeleteFileResponse.model_validate(data)

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------
    def get_tasks(
        self,
        channel_id: UUID | None = None,
        active: bool | None = None,
        status: list[str] | None = None,
        limit: int | None = None,
    ) -> list[TaskOut]:
        params: dict[str, Any] = {}
        if channel_id:
            params["channel_id"] = str(channel_id)
        if active is not None:
            params["active"] = "true" if active else "false"
        if status:
            params["status"] = ",".join(status)
        if limit is not None:
            params["limit"] = str(int(limit))
        data = self._request("GET", "/tasks", params=params)
        return [TaskOut.model_validate(t) for t in data]

    def get_task(self, task_id: UUID) -> TaskOut:
        data = self._request("GET", f"/tasks/{task_id}")
        return TaskOut.model_validate(data)

    # ------------------------------------------------------------------
    # SSE / Updates
    # ------------------------------------------------------------------
    def open_sse(self, subscriber_id: str | None = None) -> httpx.Response:
        params = {}
        if subscriber_id:
            params["subscriber_id"] = subscriber_id
        url = f"{self.base_url}/updates/sse"
        return self._client.stream(
            "GET",
            url,
            headers={**self._headers(), "Accept": "text/event-stream"},
            params=params,
            timeout=None,
        )

    def ack_events(self, subscriber_id: str, event_ids: list[str]) -> AckResponse:
        payload = {"subscriber_id": subscriber_id, "event_ids": event_ids}
        data = self._request("POST", "/updates/ack", json=payload)
        return AckResponse.model_validate(data)

    def close(self) -> None:
        self._client.close()
