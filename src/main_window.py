from __future__ import annotations

import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from src.api_client import ApiClient
from src.api_worker import run_api, cancel_api_requests
from src.auth_manager import AuthManager
from src.browser_manager import BrowserManager
from src.models import AICustomProviderOut, AITypeOut, AiPromptsOut, ChannelOut, ProjectOut, ProxyOut, TaskOut
from src.settings import AppSettings
from src.sse_client import SseWorker
from src.widgets.add_channel_dialog import AddChannelDialog
from src.widgets.auth_tags_dialog import AuthTagsDialog
from src.widgets.avatar_loader import AvatarLoader
from src.widgets.channels_page import ChannelsPage
from src.widgets.dashboard import DashboardWidget
from src.widgets.models_page import ModelsPage
from src.widgets.projects_page import ProjectsPage
from src.widgets.proxies_page import ProxiesPage
from src.widgets.settings_dialog import SettingsDialog
from src.widgets.sidebar import Sidebar
from src.widgets.tasks_page import TasksPage


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("YouTube Uploader Client")
        self.resize(1400, 900)

        self.settings = AppSettings()
        if self.settings.window_geometry:
            self.restoreGeometry(self.settings.window_geometry)

        self.api: ApiClient | None = None
        self.browser_manager = BrowserManager(self.settings._dir, self)
        self.browser_manager.error.connect(
            lambda message: QMessageBox.critical(self, "Browser error", message)
        )
        self.sse: SseWorker | None = None
        self._sse_graveyard: list[SseWorker] = []
        self.auth_manager = AuthManager(self)
        self.auth_manager.code_received.connect(self._on_auth_code)
        self.auth_manager.error_occurred.connect(self._on_auth_error)
        self.auth_manager.authorization_timed_out.connect(self._on_auth_timeout)
        self.auth_manager.authorization_closed.connect(self._on_auth_closed)

        self._channels: list[ChannelOut] = []
        self._projects: list[ProjectOut] = []
        self._proxies: list[ProxyOut] = []
        self._pending_state: str | None = None
        self._auth_use_storage_state = False

        self._avatar_loader: AvatarLoader | None = None
        self._pending_task_updates: dict[str, TaskOut] = {}
        self._task_update_timer = QTimer(self)
        self._task_update_timer.setSingleShot(True)
        self._task_update_timer.timeout.connect(self._flush_task_updates)
        self._channel_refresh_timer = QTimer(self)
        self._channel_refresh_timer.setSingleShot(True)
        self._channel_refresh_timer.timeout.connect(self._refresh_channels_snapshot)
        self._sse_started = False
        self._start_sse_after_load = False

        self._build_ui()
        self._build_tray()
        self._connect_api()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = Sidebar(self)
        self.sidebar.set_last_browser_proxy(self.settings.last_proxy_url)
        self.sidebar.page_changed.connect(self._on_page_changed)
        self.sidebar.settings_clicked.connect(self._open_settings)
        self.sidebar.add_channel_clicked.connect(self._start_add_channel)
        self.sidebar.browser_clicked.connect(self._open_common_browser)

        self.stack = QStackedWidget()
        self.dashboard = DashboardWidget()
        self.channels_page = ChannelsPage()
        self.channels_page.uploaded_count_changed.connect(self.dashboard.set_uploaded_count)
        self.channels_page.tags_changed.connect(self._on_single_channel_updated)
        self.channels_page.channel_auth_requested.connect(self._start_reauth)
        self.channels_page.channel_deleted.connect(self._remove_channel_from_list)
        self.tasks_page = TasksPage()
        self.projects_page = ProjectsPage()
        self.proxies_page = ProxiesPage()
        self.models_page = ModelsPage(self.settings)

        self.stack.addWidget(self.dashboard)
        self.stack.addWidget(self.channels_page)
        self.stack.addWidget(self.tasks_page)
        self.stack.addWidget(self.projects_page)
        self.stack.addWidget(self.proxies_page)
        self.stack.addWidget(self.models_page)

        splitter = QWidget()
        h = QVBoxLayout(splitter)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)

        hl = QHBoxLayout()
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        hl.addWidget(self.sidebar)
        hl.addWidget(self.stack, 1)
        h.addLayout(hl)
        layout.addWidget(splitter)

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        from PySide6.QtWidgets import QStyle
        style = self.style()
        icon = style.standardIcon(getattr(QStyle, 'SP_ComputerIcon', QStyle.SP_MessageBoxInformation))
        self.tray.setIcon(icon)
        self.tray.setVisible(True)
        self.tray.activated.connect(self._tray_activated)

        from PySide6.QtWidgets import QMenu

        self._tray_menu = QMenu(self)
        show_action = QAction("Show", self)
        show_action.triggered.connect(self.showNormal)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit)
        self._tray_menu.addAction(show_action)
        self._tray_menu.addSeparator()
        self._tray_menu.addAction(quit_action)
        self.tray.setContextMenu(self._tray_menu)

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.DoubleClick:
            self.showNormal()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.settings.window_geometry = self.saveGeometry().data()
        self.settings.save()
        if self.tray.isVisible():
            self.hide()
            event.ignore()
        else:
            event.accept()

    def _quit(self) -> None:
        self.browser_manager.close_all()
        self._disconnect_api()
        for worker in list(self._sse_graveyard):
            worker.wait(2000)
        QApplication.quit()

    # ------------------------------------------------------------------
    # API connection
    # ------------------------------------------------------------------
    def _connect_api(self) -> None:
        if not self.settings.server_url or not self.settings.api_token:
            self._open_settings()
            return
        self.api = ApiClient(self.settings.server_url, self.settings.api_token)
        self.channels_page.set_api(self.api)
        self.tasks_page.set_api(self.api)
        self.projects_page.set_api(self.api)
        self.proxies_page.set_api(self.api)
        self.models_page.set_api(self.api)
        self._avatar_loader = AvatarLoader(self.api, self)
        self._avatar_loader.avatar_loaded.connect(self._on_avatar_loaded)
        self._start_sse_after_load = True
        self._load_data()

    def _disconnect_api(self) -> None:
        if self.sse:
            sse = self.sse
            self.sse = None
            try:
                sse.connection_opened.disconnect(self._on_sse_opened)
                sse.event_received.disconnect(self._on_sse_event)
                sse.disconnected.disconnect(self._on_sse_disconnected)
                sse.reconnected.disconnect(self._on_sse_reconnected)
            except RuntimeError:
                pass
            sse.stop()
            if sse.isRunning():
                # Нельзя уничтожать QThread, пока поток ещё работает — Qt
                # аварийно завершит приложение. Держим ссылку, пока поток
                # сам не завершится.
                self._sse_graveyard.append(sse)
                sse.finished.connect(lambda worker=sse: self._reap_sse(worker))
        self._sse_started = False
        if self.api:
            cancel_api_requests(self.api)
            run_api(self.api, self.api.close)
            self.api = None

    def _reap_sse(self, worker: SseWorker) -> None:
        if worker in self._sse_graveyard:
            self._sse_graveyard.remove(worker)
        worker.deleteLater()

    def open_browser(
        self,
        profile_name: str,
        proxy_id: str | None = None,
        initial_url: str = "https://studio.youtube.com/",
    ) -> None:
        proxy_url = None
        if proxy_id:
            proxy = next((item for item in self._proxies if str(item.id) == str(proxy_id)), None)
            if proxy is None:
                QMessageBox.critical(self, "Proxy error", "The selected proxy is deleted or was not saved.")
                return
            proxy_url = proxy.url
            if profile_name == "common":
                self.settings.last_proxy_id = str(proxy.id)
                self.settings.last_proxy_url = proxy.url
                self.settings.save()
                self.sidebar.set_last_browser_proxy(proxy.url)
        elif profile_name == "common":
            self.settings.last_proxy_id = None
            self.settings.last_proxy_url = None
            self.settings.save()
            self.sidebar.set_last_browser_proxy(None)
        self.browser_manager.open(profile_name, proxy_url, initial_url)

    def _open_common_browser(self) -> None:
        from src.widgets.browser_proxy_dialog import BrowserProxyDialog

        dialog = BrowserProxyDialog(self._proxies, self.settings, self)
        if dialog.exec() == dialog.DialogCode.Accepted:
            self.open_browser("common", dialog.proxy_id())

    def _load_data(self) -> None:
        if not self.api:
            self.sidebar.set_sse_connected(False)
            return
        # Загружаем каналы, проекты и активные задачи параллельно
        self._pending_loads = 7
        self._load_error = False

        run_api(self.api, self.api.get_channels, self._on_channels_loaded, self._on_load_error, parent=self)
        run_api(self.api, self.api.get_projects, self._on_projects_loaded, self._on_load_error, parent=self)
        run_api(self.api, self.api.get_tasks, self._on_tasks_loaded, self._on_load_error, active=True, limit=200, parent=self)
        run_api(self.api, self.api.get_proxies, self._on_proxies_loaded, self._on_load_error, parent=self)
        run_api(self.api, self.api.get_ai_types, self._on_ai_types_loaded, self._on_load_error, parent=self)
        run_api(self.api, self.api.get_ai_providers, self._on_ai_providers_loaded, self._on_load_error, parent=self)
        run_api(self.api, self.api.get_ai_prompts, self._on_ai_prompts_loaded, self._on_load_error, parent=self)

    def _on_channels_loaded(self, channels: list[ChannelOut]) -> None:
        operations = {channel.id: channel.loading_operations.copy() for channel in self._channels}
        for channel in channels:
            channel.loading_operations = operations.get(channel.id, set())
        self._channels = channels
        self.channels_page.set_channels(channels)
        self.tasks_page.set_channels(channels)
        self.dashboard.set_channels(channels)
        if self._avatar_loader:
            for ch in channels:
                self._avatar_loader.load(ch.id)
        self._check_load_complete()

    def _on_projects_loaded(self, projects: list[ProjectOut]) -> None:
        self._projects = projects
        self.projects_page.set_projects(projects)
        self._check_load_complete()

    def _on_tasks_loaded(self, tasks: list[TaskOut]) -> None:
        self.dashboard.set_active_uploads(tasks)
        self.tasks_page.set_tasks(tasks)
        self._check_load_complete()

    def _on_proxies_loaded(self, proxies: list[ProxyOut]) -> None:
        self._proxies = proxies
        self.channels_page.set_proxies(proxies)
        self.proxies_page.set_proxies(proxies)
        self._check_load_complete()

    def _on_ai_types_loaded(self, types: list[AITypeOut]) -> None:
        self.models_page.set_ai_types(types)
        self.channels_page.set_ai_types(types)
        self._check_load_complete()

    def _on_ai_providers_loaded(self, providers: list[AICustomProviderOut]) -> None:
        self.models_page.set_ai_providers(providers)
        self.channels_page.set_ai_providers(providers)
        self._check_load_complete()

    def _on_ai_prompts_loaded(self, prompts: AiPromptsOut) -> None:
        self.models_page.set_prompts(prompts)
        self._check_load_complete()

    def _on_load_error(self, message: str) -> None:
        self._load_error = True
        self.sidebar.set_sse_connected(False)
        self.dashboard.add_event(f"Connection failed: {message}", "error")
        self._check_load_complete()

    def _check_load_complete(self) -> None:
        self._pending_loads -= 1
        if self._pending_loads <= 0 and self._start_sse_after_load and not self._sse_started:
            self._start_sse_after_load = False
            self._start_sse()
        if self._pending_loads <= 0 and self._load_error:
            # Все запросы завершены, но были ошибки — показываем тихое уведомление
            pass

    # ------------------------------------------------------------------
    # SSE
    # ------------------------------------------------------------------
    def _start_sse(self) -> None:
        if not self.api or self._sse_started:
            return
        self.sse = SseWorker(self.settings.server_url, self.settings.api_token, self.settings.subscriber_id)
        self.sse.connection_opened.connect(self._on_sse_opened)
        self.sse.event_received.connect(self._on_sse_event)
        self.sse.disconnected.connect(self._on_sse_disconnected)
        self.sse.reconnected.connect(self._on_sse_reconnected)
        self.sse.start()
        self._sse_started = True

    def _on_sse_opened(self, subscriber_id: str, pending_count: int) -> None:
        self.settings.subscriber_id = subscriber_id
        self.settings.save()
        self.sidebar.set_sse_state("connected")
        # Replayed events may describe an old state. Reconcile once after the
        # replay burst so the server snapshot is the final source of truth.
        if pending_count:
            self._channel_refresh_timer.start(750)

    def _on_sse_disconnected(self, reason: str) -> None:
        self.sidebar.set_sse_state("connecting")

    def _on_sse_reconnected(self) -> None:
        """Соединение восстановлено после обрыва — перезагружаем все данные."""
        print("[SSE] Reconnected — refreshing all data")
        self.channels_page._refresh_counts_snapshot = True
        self.sidebar.set_sse_state("connected")
        self.dashboard.add_event("Connection restored — refreshing data", "info")
        # Полная перезагрузка каналов, проектов и активных задач.
        self._load_data()
        # Обновляем открытые таблицы выбранного канала
        if self.channels_page._current_channel:
            self.channels_page._files_tab._load_files()
            self.channels_page._tasks_tab._load()

    def _queue_channel_refresh(self) -> None:
        self._channel_refresh_timer.start(250)

    def _refresh_channels_snapshot(self) -> None:
        if self.api:
            run_api(self.api, self.api.get_channels, self._on_channels_loaded, lambda message: None, parent=self)

    def _on_sse_event(self, event_name: str, payload: dict) -> None:
        if event_name == "connection.opened":
            sid = payload.get("subscriber_id", "")
            pending = payload.get("pending_count", 0)
            print(f"[SSE UI] connection opened: {sid}, pending={pending}")
            return

        try:
            if event_name in (
                "channel.updated",
                "channel.connected",
                "channel.reauth_required",
                "channel.session_expired",
                "channel.metadata_updated",
            ):
                channel_data = dict(payload.get("channel") or {})
                if event_name == "channel.metadata_updated":
                    # Metadata events carry the refreshed snippet separately.
                    # Overlay it so metadata_updated_at cannot be lost when the
                    # channel snapshot is older or omits that field.
                    channel_data.update(payload.get("metadata") or {})
                ch = ChannelOut.model_validate(channel_data)
                self.channels_page.update_channel(ch)
                self.channels_page.refresh_uploaded_count_if_channel(ch.id)
                self.dashboard.update_channel(ch)
                self._update_channel_in_list(ch)
                ch_name = ch.youtube_title or ch.title or "Unknown"
                if event_name == "channel.reauth_required":
                    self.dashboard.add_event(f"Channel {ch_name} needs re-authorization", "error")
                    self._tray_notify("Re-authorization required", f"Channel {ch_name}")
                elif event_name == "channel.session_expired":
                    msg = payload.get("message", "")
                    self.dashboard.add_event(f"Channel {ch_name} session expired: {msg}", "error")
                    self._tray_notify("Session expired", f"Channel {ch_name}")

            elif event_name == "channel.unpaused":
                cid = payload.get("channel_id", "")
                ch_data = payload.get("channel")
                if ch_data:
                    try:
                        ch = ChannelOut.model_validate(ch_data)
                    except Exception as exc:
                        print(f"[SSE UI] channel.unpaused validation error: {exc}")
                        ch = None
                else:
                    ch = None
                if ch:
                    self.channels_page.update_channel(ch)
                    self.channels_page.refresh_uploaded_count_if_channel(ch.id)
                    self.dashboard.update_channel(ch)
                    self._update_channel_in_list(ch)
                elif self.api and cid:
                    self._queue_channel_refresh()
                if cid:
                    self.channels_page.refresh_uploaded_count_if_channel(cid)
                ch_name = (ch.youtube_title or ch.title or "Unknown") if ch else f"{cid}"
                self.dashboard.add_event(f"Channel {ch_name} unpaused", "success")

            elif event_name == "channel.deleted":
                # YouTube removal is a soft delete and carries ChannelOut.
                ch_data = payload.get("channel")
                if not ch_data and payload.get("status") == "deleted":
                    ch_data = payload
                if ch_data:
                    ch = ChannelOut.model_validate(ch_data)
                    self.browser_manager.remove_channel_profile(ch.id)
                    self.channels_page.update_channel(ch)
                    self.dashboard.update_channel(ch)
                    self._update_channel_in_list(ch)
                else:
                    cid = payload.get("channel_id")
                    if cid:
                        self.channels_page.remove_channel(cid)
                        self._remove_channel_from_list(cid)

            elif event_name == "channel.paused":
                cid = payload.get("channel_id", "")
                reason = payload.get("reason", "")
                minutes = payload.get("minutes", 0)
                self.dashboard.add_event(f"Channel {cid} paused ({reason}, {minutes} min)", "warning")
                # Update from payload if channel object present, else fetch
                ch_data = payload.get("channel")
                if ch_data:
                    try:
                        ch = ChannelOut.model_validate(ch_data)
                        self.channels_page.update_channel(ch)
                        self.channels_page.refresh_uploaded_count_if_channel(ch.id)
                        self.dashboard.update_channel(ch)
                        self._update_channel_in_list(ch)
                    except Exception as exc:
                        print(f"[SSE UI] channel.paused validation error: {exc}")
                elif self.api and cid:
                    self._queue_channel_refresh()
                if cid:
                    self.channels_page.refresh_uploaded_count_if_channel(cid)
                if not ch_data:
                    self._queue_channel_refresh()

            elif event_name == "channel.error":
                cid = payload.get("channel_id", "")
                error = payload.get("error", "")
                self.dashboard.add_event(f"Channel {cid} error: {error}", "error")
                self._tray_notify("Channel error", error)
                ch_data = payload.get("channel")
                if ch_data:
                    try:
                        ch = ChannelOut.model_validate(ch_data)
                        self.channels_page.update_channel(ch)
                        self.dashboard.update_channel(ch)
                        self._update_channel_in_list(ch)
                    except Exception as exc:
                        print(f"[SSE UI] channel.error validation error: {exc}")
                elif self.api and cid:
                    self._queue_channel_refresh()
                if not ch_data:
                    self._queue_channel_refresh()

            elif event_name == "channel.avatar_updated":
                cid = payload.get("channel_id", "")
                if self._avatar_loader and cid:
                    from uuid import UUID
                    self._avatar_loader.load(UUID(cid))

            elif event_name in ("task.updated", "task.created", "task.completed", "task.failed"):
                raw_task = payload.get("task", {})
                print(f"[SSE UI] {event_name} raw_task channel_id={raw_task.get('channel_id')}, id={raw_task.get('id')}")
                task = TaskOut.model_validate(raw_task)
                print(f"[SSE UI] {event_name} validated task.id={task.id} channel_id={task.channel_id}")
                self._queue_task_update(task)
                if event_name == "task.completed":
                    self.channels_page.refresh_uploaded_count_if_channel(task.channel_id)

            elif event_name == "tasks.created":
                tasks = payload.get("tasks", [])
                cid = payload.get("channel_id", "")
                for t_data in tasks:
                    try:
                        task = TaskOut.model_validate(t_data)
                        self._queue_task_update(task)
                    except Exception as exc:
                        print(f"[SSE UI] tasks.created validation error: {exc}")
                if cid:
                    self.channels_page.refresh_uploaded_count_if_channel(cid)

            elif event_name == "task.cancelled":
                tid = payload.get("task_id")
                if tid:
                    self.tasks_page.remove_task(tid)
                    self.channels_page.remove_task(tid)

            elif event_name == "tasks.cancelled":
                cid = payload.get("channel_id", "")
                reason = payload.get("reason", "")
                self.dashboard.add_event(f"Tasks cancelled for channel {cid}: {reason}", "warning")
                self.tasks_page.refresh()
                self.channels_page.refresh_tasks_if_channel(cid)

            elif event_name == "video.rejected":
                task = TaskOut.model_validate(payload.get("task", {}))
                reason = payload.get("reason", "")
                self._queue_task_update(task)
                self.dashboard.add_event(f"Video rejected: {reason}", "error")

            elif event_name == "video.blocked":
                # The task remains completed, but the server has subsequently
                # confirmed an all-country copyright block, deleted the video
                # from YouTube, and registered its hash for future skipping.
                task = TaskOut.model_validate(payload.get("task", {}))
                video_id = payload.get("video_id", task.youtube_video_id or "")
                reason = payload.get("reason", "copyright block")
                self._queue_task_update(task)
                self.dashboard.add_event(
                    f"Video {video_id} was removed: {reason}", "warning"
                )
                self._tray_notify("Copyright-blocked video removed", str(video_id))

            elif event_name == "video.deleted":
                cid = payload.get("channel_id", "")
                vid = payload.get("video_id", "")
                self.dashboard.add_event(f"Video {vid} deleted from channel {cid}", "warning")

            elif event_name == "uploaded_hash.deleted":
                cid = payload.get("channel_id", "")
                vid = payload.get("video_id", "")
                self.dashboard.add_event(f"Hash deleted for video {vid} on channel {cid}", "info")

            elif event_name == "file.deleted":
                cid = payload.get("channel_id", "")
                file_name = payload.get("file_name", "")
                self.dashboard.add_event(f"File {file_name} deleted from channel {cid}", "info")
                self.channels_page.refresh_files_if_channel(cid)

            elif event_name == "ai_type.updated":
                if payload.get("ai_type"):
                    try:
                        ai_type = AITypeOut.model_validate(payload["ai_type"])
                        self.models_page.update_ai_type(ai_type)
                        self.channels_page.update_ai_type(ai_type)
                    except Exception as exc:
                        print(f"[SSE UI] ai_type.updated validation error: {exc}")

            elif event_name == "ai_type.deleted":
                api_type = payload.get("api_type")
                if api_type:
                    self.models_page.on_ai_type_deleted(api_type)
                    self.channels_page.on_ai_type_deleted(api_type)

            elif event_name in ("ai_provider.created", "ai_provider.updated"):
                if payload.get("ai_provider"):
                    try:
                        provider = AICustomProviderOut.model_validate(payload["ai_provider"])
                        self.models_page.update_ai_provider(provider)
                        self.channels_page.update_ai_provider(provider)
                    except Exception as exc:
                        print(f"[SSE UI] {event_name} validation error: {exc}")

            elif event_name == "ai_provider.deleted":
                provider_id = payload.get("ai_provider_id")
                if provider_id:
                    self.models_page.remove_ai_provider(provider_id)
                    self.channels_page.remove_ai_provider(provider_id)

            elif event_name == "ai_prompts.updated":
                self.models_page.update_prompts(
                    payload.get("title_prompt", ""),
                    payload.get("description_prompt", ""),
                )

            elif event_name in ("project.created", "project.updated", "project.deleted"):
                if self.api:
                    run_api(self.api, self.api.get_projects, self._on_projects_loaded, lambda e: None, parent=self)

            elif event_name in (
                "proxy.created",
                "proxy.updated",
                "proxy.deleted",
                "proxies.imported",
                "proxies.checked",
                "channel.proxy_updated",
                "channel.proxy_switched",
            ):
                if event_name == "channel.proxy_updated" and payload.get("channel"):
                    try:
                        channel = ChannelOut.model_validate(payload["channel"])
                        self.channels_page.update_channel(channel)
                        self.dashboard.update_channel(channel)
                        self._update_channel_in_list(channel)
                    except Exception as exc:
                        print(f"[SSE UI] channel proxy validation error: {exc}")
                elif event_name == "channel.proxy_switched" and payload.get("channel_id"):
                    self._queue_channel_refresh()
                if event_name == "proxies.checked" and payload.get("proxies"):
                    try:
                        checked = [ProxyOut.model_validate(item) for item in payload["proxies"]]
                        for proxy in checked:
                            self._update_proxy(proxy)
                    except Exception as exc:
                        print(f"[SSE UI] proxy check validation error: {exc}")
                elif event_name in ("proxy.updated", "proxy.created") and payload.get("proxy"):
                    try:
                        updated = ProxyOut.model_validate(payload["proxy"])
                        self._update_proxy(updated)
                    except Exception as exc:
                        print(f"[SSE UI] proxy validation error: {exc}")
                elif event_name == "proxy.deleted" and payload.get("proxy_id"):
                    self._remove_proxy(payload["proxy_id"])
                elif self.api:
                    run_api(self.api, self.api.get_proxies, self._on_proxies_loaded, lambda e: None, parent=self)

        except Exception as exc:
            print(f"[SSE UI] Error handling {event_name}: {exc}")

    def _update_proxy(self, proxy: ProxyOut) -> None:
        for index, current in enumerate(self._proxies):
            if current.id == proxy.id:
                if current == proxy:
                    return
                self._proxies[index] = proxy
                break
        else:
            self._proxies.append(proxy)
        self.proxies_page.update_proxy(proxy)
        self.channels_page.update_proxy(proxy)

    def _remove_proxy(self, proxy_id) -> None:
        self._proxies = [proxy for proxy in self._proxies if str(proxy.id) != str(proxy_id)]
        self.proxies_page.remove_proxy(proxy_id)
        self.channels_page.remove_proxy(proxy_id)

    def _update_channel_in_list(self, channel: ChannelOut) -> None:
        for i, c in enumerate(self._channels):
            if c.id == channel.id:
                self._channels[i] = channel
                return
        self._channels.append(channel)
        self.tasks_page.set_channels(self._channels)

    def _on_single_channel_updated(self, channel: ChannelOut) -> None:
        self.channels_page.update_channel(channel)
        self.dashboard.update_channel(channel)
        self._update_channel_in_list(channel)
        if self._avatar_loader:
            self._avatar_loader.load(channel.id)

    def _remove_channel_from_list(self, channel_id) -> None:
        self.browser_manager.remove_channel_profile(channel_id)
        sid = str(channel_id)
        self._channels = [c for c in self._channels if str(c.id) != sid]
        self.channels_page.set_channels(self._channels)
        self.tasks_page.set_channels(self._channels)
        self.dashboard.set_channels(self._channels)

    def _on_avatar_loaded(self, channel_id, pixmap: QPixmap) -> None:
        self.channels_page.set_channel_avatar(str(channel_id), pixmap)
        self.dashboard.set_channel_avatar(str(channel_id), pixmap)

    def _queue_task_update(self, task: TaskOut) -> None:
        self._pending_task_updates[str(task.id)] = task
        if not self._task_update_timer.isActive():
            self._task_update_timer.start(100)

    def _flush_task_updates(self) -> None:
        if not self._pending_task_updates:
            return
        tasks = list(self._pending_task_updates.values())
        self._pending_task_updates.clear()
        self.dashboard.update_tasks(tasks)
        self.tasks_page.update_tasks(tasks)
        self.channels_page.update_tasks(tasks)

    def _on_page_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if index == 3:  # Projects
            self.projects_page.refresh()
        elif index == 4:  # Proxies
            self.proxies_page.refresh()
        elif index == 5:  # Models
            self.models_page.refresh()
        elif index == 2:  # Tasks
            self.tasks_page.refresh()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec() != SettingsDialog.Accepted:
            return
        self._disconnect_api()
        # Проверяем новые URL/токен отдельным клиентом. Таймаут должен быть
        # заметно больше, чем у SSE-хендшейка: /projects обращается к БД и на
        # удалённом сервере легко превышает несколько секунд, из-за чего
        # проверка падала по read timeout при рабочих настройках.
        test_client = ApiClient(self.settings.server_url, self.settings.api_token, timeout=60.0)

        def on_test_ok(_):
            run_api(test_client, test_client.close)
            self._connect_api()

        def on_test_err(msg: str):
            run_api(test_client, test_client.close)
            self.sidebar.set_sse_connected(False)
            QMessageBox.critical(
                self,
                "Connection Error",
                f"Could not connect with the new settings:\n{msg}",
            )

        run_api(test_client, test_client.get_projects, on_test_ok, on_test_err, parent=self)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def _start_add_channel(self) -> None:
        if not self.api:
            return
        if not self._projects:
            QMessageBox.information(self, "Info", "No projects available. Create a project first.")
            return
        dlg = AddChannelDialog(self._projects, self._channels, self._proxies, self)
        if dlg.exec() == AddChannelDialog.Accepted:
            payload = dlg.payload()
            # Server requires channel_title for new channels even though API says optional
            payload.setdefault("channel_title", "New Channel")
            self._do_auth(
                channel=None,
                extra_payload=payload,
                use_storage_state=dlg.use_storage_state(),
            )

    def _start_reauth(self, channel: ChannelOut) -> None:
        if not self.api:
            return
        self._do_auth(channel=channel)

    def _do_auth(
        self,
        channel: ChannelOut | None = None,
        extra_payload: dict | None = None,
        use_storage_state: bool = False,
    ) -> None:
        if not self.api:
            return
        # Открывать ли OAuth-окно с сохранённым состоянием общего браузера.
        self._auth_use_storage_state = use_storage_state
        payload = dict(extra_payload) if extra_payload else {}
        if channel:
            payload["channel_id"] = str(channel.id)
        self._pending_auth_channel = channel
        self._auth_started_at = time.monotonic()
        # Запрос к серверу может быть небыстрым (проверка прокси и т.п.) —
        # показываем прогресс, пока authorization_url не получен.
        self._show_auth_progress()
        run_api(
            self.api,
            self.api.start_auth,
            self._on_auth_started,
            self._on_auth_start_error,
            payload,
            parent=self,
        )

    def _show_auth_progress(self) -> None:
        from PySide6.QtWidgets import QProgressDialog

        self._close_auth_progress()
        progress = QProgressDialog("Starting authorization...", None, 0, 0, self)
        progress.setWindowTitle("Authorization")
        progress.setModal(True)
        progress.setMinimumDuration(0)
        progress.show()
        self._auth_progress = progress

    def _close_auth_progress(self) -> None:
        progress = getattr(self, "_auth_progress", None)
        self._auth_progress = None
        if progress is not None:
            progress.cancel()
            progress.close()
            progress.deleteLater()

    def _on_auth_started(self, resp) -> None:
        started = getattr(self, "_auth_started_at", None)
        if started is not None:
            print(f"[Auth] server returned authorization_url in {time.monotonic() - started:.1f}s")
        self._close_auth_progress()
        self._pending_state = resp.state
        user_data_dir = None
        if getattr(self, "_auth_use_storage_state", False):
            user_data_dir = self.browser_manager.prepare_auth_webview_state()
            if user_data_dir is None:
                self.setEnabled(True)
                self.activateWindow()
                QMessageBox.critical(
                    self,
                    "Storage state",
                    "Failed to prepare the authorization window with the saved "
                    "storage state. Try signing in manually.",
                )
                self._clear_pending_reauth()
                return
        self.setEnabled(False)
        self.auth_manager.open_browser(
            resp.authorization_url,
            resp.proxy_settings,
            resp.authorization_timeout_seconds,
            str(user_data_dir) if user_data_dir else None,
        )

    def _on_auth_start_error(self, message: str) -> None:
        self._close_auth_progress()
        self.setEnabled(True)
        self.activateWindow()
        QMessageBox.critical(self, "Auth Error", message)
        if getattr(self, "_pending_auth_channel", None):
            self.channels_page.set_channel_operation_loading(self._pending_auth_channel.id, "reauth", False)

    def _on_auth_code(self, code: str, state: str) -> None:
        self.auth_manager.stop()
        self.setEnabled(True)
        self.activateWindow()
        if not self.api:
            return
        from src.models import AuthCallbackPayload

        dialog = AuthTagsDialog(self.api, getattr(self, "_pending_auth_channel", None), self)
        if dialog.exec() != AuthTagsDialog.Accepted:
            self._clear_pending_reauth()
            return

        run_api(
            self.api,
            self.api.callback_auth,
            self._on_auth_callback_ok,
            self._on_auth_callback_error,
            AuthCallbackPayload(state=state, code=code, tag_ids=dialog.tag_ids()),
            parent=self,
        )

    def _on_auth_timeout(self) -> None:
        self.auth_manager.stop()
        self.setEnabled(True)
        self.activateWindow()
        self._clear_pending_reauth()
        QMessageBox.critical(self, "Auth Error", "Authorization timed out.")

    def _on_auth_closed(self) -> None:
        self.setEnabled(True)
        self.activateWindow()
        self._clear_pending_reauth()

    def _clear_pending_reauth(self) -> None:
        if getattr(self, "_pending_auth_channel", None):
            self.channels_page.set_channel_operation_loading(
                self._pending_auth_channel.id,
                "reauth",
                False,
            )

    def _on_auth_callback_ok(self, channel: ChannelOut) -> None:
        QMessageBox.information(self, "Success", "Channel authorized successfully!")
        self._load_data()
        if getattr(self, "_pending_auth_channel", None):
            self.channels_page.set_channel_operation_loading(self._pending_auth_channel.id, "reauth", False)

    def _on_auth_callback_error(self, message: str) -> None:
        QMessageBox.critical(self, "Auth Error", message)
        if getattr(self, "_pending_auth_channel", None):
            self.channels_page.set_channel_operation_loading(self._pending_auth_channel.id, "reauth", False)

    def _on_auth_error(self, message: str) -> None:
        self.auth_manager.stop()
        self.setEnabled(True)
        self.activateWindow()
        QMessageBox.critical(self, "Auth Error", message)
        if getattr(self, "_pending_auth_channel", None):
            self.channels_page.set_channel_operation_loading(self._pending_auth_channel.id, "reauth", False)

    def _tray_notify(self, title: str, message: str) -> None:
        if self.tray.isVisible():
            self.tray.showMessage(title, message, QSystemTrayIcon.Critical, 5000)
