import json
from pathlib import Path

from platformdirs import user_data_dir


class AppSettings:
    def __init__(self) -> None:
        self.app_name = "YouTubeUploaderClient"
        self.app_author = "UploaderClient"
        self._dir = Path(user_data_dir(self.app_name, self.app_author))
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "settings.json"

        self.server_url: str = "http://localhost:8000"
        self.api_token: str = ""
        self.subscriber_id: str | None = None
        self.window_geometry: bytes | None = None
        self.last_proxy_id: str | None = None
        self.last_proxy_url: str | None = None
        # Полные значения API-ключей нейросетей: сервер возвращает ключи только
        # в маскированном виде (first4...last4), а PUT /ai-types заменяет весь
        # список — без локальной копии нельзя добавить/удалить отдельный ключ.
        self.ai_key_vault: dict[str, list[str]] = {}

        self.load()

    def load(self) -> None:
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            self.server_url = data.get("server_url", self.server_url)
            self.api_token = data.get("api_token", "")
            self.subscriber_id = data.get("subscriber_id")
            self.last_proxy_id = data.get("last_proxy_id")
            self.last_proxy_url = data.get("last_proxy_url")
            vault = data.get("ai_key_vault")
            if isinstance(vault, dict):
                self.ai_key_vault = {
                    str(api_type): [str(k) for k in keys if isinstance(k, str) and k]
                    for api_type, keys in vault.items()
                    if isinstance(keys, list)
                }
            geom = data.get("window_geometry")
            if geom:
                self.window_geometry = bytes.fromhex(geom)
        except Exception:
            pass

    def save(self) -> None:
        data = {
            "server_url": self.server_url,
            "api_token": self.api_token,
            "subscriber_id": self.subscriber_id,
            "last_proxy_id": self.last_proxy_id,
            "last_proxy_url": self.last_proxy_url,
            "ai_key_vault": self.ai_key_vault,
        }
        if self.window_geometry:
            data["window_geometry"] = self.window_geometry.hex()
        self._file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
