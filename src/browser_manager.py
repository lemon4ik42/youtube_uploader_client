from __future__ import annotations

import sys
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal


class BrowserManager(QObject):
    """Starts the standalone EdgeChromium browser process."""

    error = Signal(str)

    # Storage-state entries (relative to the profile's Default directory)
    # that carry the signed-in browser session: cookies (inside the Network
    # directory on current Edge builds, or Default/Cookies on older ones)
    # plus local/session site storage.
    _STATE_FILES = ("Cookies", "Cookies-journal")
    _STATE_DIRS = ("Network", "Local Storage", "Session Storage")
    # Файлы в КОРНЕ профиля (не в Default/). "Local State" хранит ключ os_crypt,
    # которым Edge шифрует cookies и site storage. Без него скопированное в другой
    # каталог состояние не расшифровывается — Edge молча отбрасывает cookies,
    # и аккаунт "не появляется".
    _STATE_ROOT_FILES = ("Local State",)
    _COMMON_STATE_DIRNAME = "common_state"
    _AUTH_WEBVIEW_DIRNAME = "auth_webview"

    def __init__(self, data_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self._data_dir = data_dir
        self._processes: list[QProcess] = []
        self._process_profiles: dict[QProcess, Path] = {}
        self._pending_profile_deletes: set[Path] = set()

    def open(
        self,
        profile_name: str,
        proxy_url: str | None = None,
        initial_url: str = "https://studio.youtube.com/",
    ) -> None:
        if profile_name == "common":
            # The common browser must never *reuse* cookies, passwords, or
            # account state between launches: it always starts from a fresh
            # temporary profile. When it closes, its storage state is archived
            # (see _remove) so it can later be applied to a channel profile;
            # the snapshot itself is never restored into the common browser.
            shutil.rmtree(self._data_dir / "browser_profiles" / "common", ignore_errors=True)
            profile = Path(tempfile.mkdtemp(prefix="uploader-browser-"))
        else:
            profile = self._data_dir / "browser_profiles" / profile_name
        profile.mkdir(parents=True, exist_ok=True)
        module = Path(__file__).with_name("browser_webview.py")
        process = QProcess(self)
        environment = QProcessEnvironment.systemEnvironment()
        # Do not leak flags intended for OAuth WebView2 into the standalone
        # Edge process. browser_webview.py passes an explicit proxy mode.
        environment.remove("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS")
        process.setProcessEnvironment(environment)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        temporary = profile_name == "common"
        args = [str(module), str(profile), proxy_url or "", initial_url, "temporary" if temporary else "persistent"]
        process.readyReadStandardOutput.connect(lambda: self._read_output(process))
        process.finished.connect(lambda *_args: self._remove(process, profile, temporary))
        process.start(sys.executable, args)
        self._processes.append(process)
        self._process_profiles[process] = profile

    def _read_output(self, process: QProcess) -> None:
        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in output.splitlines():
            if line.startswith("BROWSER_ERROR:"):
                self.error.emit(line[len("BROWSER_ERROR:"):].strip())

    def _remove(self, process: QProcess, profile: Path, temporary: bool = False) -> None:
        if process in self._processes:
            self._processes.remove(process)
        self._process_profiles.pop(process, None)
        process.deleteLater()
        if temporary:
            # Archive the last storage state of the common browser before the
            # temporary profile is discarded. It is only ever applied to
            # channel profiles, never restored into the common browser.
            self._save_common_state(profile)
            shutil.rmtree(profile, ignore_errors=True)
        elif profile in self._pending_profile_deletes:
            shutil.rmtree(profile, ignore_errors=True)
            self._pending_profile_deletes.discard(profile)

    def remove_channel_profile(self, channel_id) -> None:
        profile = self._data_dir / "browser_profiles" / f"channel_{channel_id}"
        shutil.rmtree(profile, ignore_errors=True)
        if profile.exists():
            self._pending_profile_deletes.add(profile)

    def close_all(self) -> None:
        for process in list(self._processes):
            process.terminate()
            if not process.waitForFinished(2000):
                process.kill()
                process.waitForFinished(1000)
        self._processes.clear()

    # ------------------------------------------------------------------
    # Common browser storage state
    # ------------------------------------------------------------------
    def _common_state_path(self) -> Path:
        return self._data_dir / "browser_profiles" / self._COMMON_STATE_DIRNAME

    def _save_common_state(self, profile: Path) -> None:
        """Snapshot cookies + site storage of the closed common browser."""
        source = profile / "Default"
        if not source.is_dir():
            return
        target = self._common_state_path()
        # Собираем снапшот во временный каталог и подменяем им старый только
        # после успешного копирования — иначе сбой посередине оставил бы
        # битый (частичный) снапшот вместо рабочего.
        tmp = target.with_name(self._COMMON_STATE_DIRNAME + ".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            tmp.mkdir(parents=True)
            for name in self._STATE_ROOT_FILES:
                src = profile / name
                if src.is_file():
                    shutil.copy2(src, tmp / name)
            for name in self._STATE_FILES:
                src = source / name
                if src.is_file():
                    shutil.copy2(src, tmp / name)
            for name in self._STATE_DIRS:
                src = source / name
                if src.is_dir():
                    shutil.copytree(src, tmp / name, dirs_exist_ok=True)
            shutil.rmtree(target, ignore_errors=True)
            tmp.rename(target)
        except OSError as exc:
            self.error.emit(f"Failed to save common browser storage state: {exc}")
            shutil.rmtree(tmp, ignore_errors=True)

    def has_common_state(self) -> bool:
        target = self._common_state_path()
        if not target.is_dir():
            return False
        # Без ключа шифрования ("Local State") снапшот нерабочий.
        if not all((target / name).is_file() for name in self._STATE_ROOT_FILES):
            return False
        return any((target / name).exists() for name in self._STATE_FILES + self._STATE_DIRS)

    def is_profile_active(self, profile_name: str) -> bool:
        profile = self._data_dir / "browser_profiles" / profile_name
        return any(existing == profile for existing in self._process_profiles.values())

    def apply_common_state(self, channel_id) -> bool:
        """Replace a channel profile's storage state with the saved common one."""
        return self.apply_common_state_to_profile(f"channel_{channel_id}")

    def apply_common_state_to_profile(self, profile_name: str) -> bool:
        """Replace a named profile's storage state with the saved common one."""
        if not self.has_common_state():
            return False
        source = self._common_state_path()
        profile_root = self._data_dir / "browser_profiles" / profile_name
        target = profile_root / "Default"
        try:
            target.mkdir(parents=True, exist_ok=True)
            for name in self._STATE_ROOT_FILES:
                dst = profile_root / name
                if dst.is_dir():
                    shutil.rmtree(dst, ignore_errors=True)
                elif dst.exists() or dst.is_symlink():
                    dst.unlink(missing_ok=True)
                src = source / name
                if src.is_file():
                    shutil.copy2(src, dst)
            for name in self._STATE_FILES:
                dst = target / name
                if dst.is_dir():
                    shutil.rmtree(dst, ignore_errors=True)
                elif dst.exists() or dst.is_symlink():
                    dst.unlink(missing_ok=True)
                src = source / name
                if src.is_file():
                    shutil.copy2(src, dst)
            for name in self._STATE_DIRS:
                dst = target / name
                if dst.is_dir():
                    shutil.rmtree(dst, ignore_errors=True)
                elif dst.exists() or dst.is_symlink():
                    dst.unlink(missing_ok=True)
                src = source / name
                if src.is_dir():
                    shutil.copytree(src, dst)
        except OSError as exc:
            self.error.emit(f"Failed to apply storage state: {exc}")
            return False
        return True

    def prepare_auth_webview_state(self) -> Path | None:
        """Seed a WebView2 user-data folder with the saved common browser state.

        Returns the folder to pass to the OAuth webview as its storage path,
        or None if there is no usable snapshot. Recreated from the snapshot on
        every call so sessions of previous authorizations never leak.
        """
        if not self.has_common_state():
            return None
        source = self._common_state_path()
        # WebView2 keeps the Chromium profile inside the EBWebView subdirectory
        # of the user-data folder.
        udf = self._data_dir / "browser_profiles" / self._AUTH_WEBVIEW_DIRNAME
        target = udf / "EBWebView"
        shutil.rmtree(udf, ignore_errors=True)
        try:
            (target / "Default").mkdir(parents=True, exist_ok=True)
            for name in self._STATE_ROOT_FILES:
                src = source / name
                if src.is_file():
                    shutil.copy2(src, target / name)
            for name in self._STATE_FILES:
                src = source / name
                if src.is_file():
                    shutil.copy2(src, target / "Default" / name)
            for name in self._STATE_DIRS:
                src = source / name
                if src.is_dir():
                    shutil.copytree(src, target / "Default" / name, dirs_exist_ok=True)
        except OSError as exc:
            self.error.emit(f"Failed to prepare authorization browser state: {exc}")
            return None
        return udf
