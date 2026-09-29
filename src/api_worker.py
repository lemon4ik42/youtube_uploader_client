from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication
import shiboken6

from src.api_client import ApiClient, ApiError


# Bulk reads cannot create thousands of OS threads or starve user actions.
_pools: dict[bool, QThreadPool] = {}
_requests: set[ApiRequest] = set()


def _pool(background: bool) -> QThreadPool:
    if background not in _pools:
        pool = QThreadPool(QApplication.instance())
        pool.setMaxThreadCount(3 if background else 6)
        if QApplication.instance():
            QApplication.instance().aboutToQuit.connect(pool.clear)
        _pools[background] = pool
    return _pools[background]


class ApiRequest(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()
    finished_success = Signal()
    finished_error = Signal(str)
    _completed = Signal(object, object)

    def __init__(self, api, on_result, on_error, owner):
        super().__init__(QApplication.instance())
        self._on_result = on_result
        self._on_error = on_error
        self._owner = owner
        self.api = api
        self.cancelled = False
        self._completed.connect(self._deliver, Qt.QueuedConnection)

    @Slot(object, object)
    def _deliver(self, value, error):
        try:
            if self.cancelled:
                return
            if self._owner is not None and not shiboken6.isValid(self._owner):
                return
            if error is None:
                self.result.emit(value)
                if self._on_result:
                    self._on_result(value)
                self.finished_success.emit()
            else:
                self.error.emit(error)
                if self._on_error:
                    self._on_error(error)
                self.finished_error.emit(error)
        finally:
            self.finished.emit()
            _requests.discard(self)
            self.deleteLater()


class _Job(QRunnable):
    def __init__(self, request, method, args, kwargs):
        super().__init__()
        self.request, self.method = request, method
        self.args, self.kwargs = args, kwargs

    def run(self):
        if self.request.cancelled:
            self.request._completed.emit(None, None)
            return
        try:
            value = self.method(*self.args, **self.kwargs)
        except ApiError as exc:
            self.request._completed.emit(None, exc.message)
        except Exception as exc:
            self.request._completed.emit(None, str(exc))
        else:
            self.request._completed.emit(value, None)


def run_api(
    api: ApiClient,
    method: Callable,
    on_result: Callable[[Any], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    *args: Any,
    parent: QObject | None = None,
    background: bool = False,
    **kwargs: Any,
) -> ApiRequest:
    """Run network work off the UI thread; deliver callbacks on the UI thread."""
    request = ApiRequest(api, on_result, on_error, parent)
    _requests.add(request)
    _pool(background).start(_Job(request, method, args, kwargs))
    return request


def cancel_api_requests(api):
    """Skip queued work and stale callbacks when switching servers."""
    for request in tuple(_requests):
        if request.api is api:
            request.cancelled = True
