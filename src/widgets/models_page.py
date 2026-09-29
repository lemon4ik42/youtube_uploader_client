from __future__ import annotations

from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.api_client import ApiClient
from src.api_worker import run_api
from src.models import AICustomProviderOut, AITypeOut, AiPromptsOut
from src.settings import AppSettings

MAX_PROMPT_LENGTH = 20000

KEY_STATUS_LABELS = {
    "working": ("Working", QColor("#2e7d32")),
    "not_working": ("Not working", QColor("#c62828")),
    "quota_exceeded": ("Quota exceeded", QColor("#e65100")),
    "unknown": ("Unknown", QColor("#9e9e9e")),
}

# Вид узла дерева: встроенный тип API или custom-провайдер (OpenAI-compatible).
TYPE_KINDS = ("type", "models", "model", "keys", "key")
PROVIDER_KINDS = ("provider", "provider_models", "provider_model", "provider_keys", "provider_key")


def mask_api_key(key: str) -> str:
    # Сервер маскирует ключи как first4...last4 — повторяем маску, чтобы
    # сопоставлять локальные полные ключи с серверным списком.
    if len(key) <= 8:
        return key
    return f"{key[:4]}...{key[-4:]}"


def provider_vault_key(provider_id) -> str:
    # Ключ локального хранилища полных значений API-ключей; совпадает с
    # форматом ссылки на провайдер в ai_model канала.
    return f"custom:{provider_id}"


class ApiKeysDialog(QDialog):
    def __init__(self, owner: str, replace: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Replace API Keys" if replace else "Add API Keys")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        if replace:
            intro = (
                f"Paste the complete list of API keys for '{owner}', one key per line.\n"
                "The list replaces every key currently stored on the server."
            )
        else:
            intro = (
                f"Paste new API keys for '{owner}', one key per line.\n"
                "They are appended to the keys currently stored on the server."
            )
        intro_label = QLabel(intro)
        intro_label.setWordWrap(True)
        layout.addWidget(intro_label)
        note = QLabel("Saving the key list resets the status of all keys to 'unknown'.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self._edit = QPlainTextEdit()
        self._edit.setPlaceholderText("sk-...")
        self._edit.setMinimumHeight(160)
        layout.addWidget(self._edit)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_validated(self) -> None:
        if not self.keys():
            QMessageBox.warning(self, "Invalid input", "Enter at least one non-empty API key.")
            return
        self.accept()

    def keys(self) -> list[str]:
        result: list[str] = []
        for line in self._edit.toPlainText().splitlines():
            key = line.strip()
            if key and key not in result:
                result.append(key)
        return result


class ProviderDialog(QDialog):
    """Создание/редактирование custom-провайдера (OpenAI-compatible)."""

    def __init__(self, provider: AICustomProviderOut | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Provider" if provider else "Add Provider")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._name = QLineEdit(provider.name if provider else "")
        self._base_url = QLineEdit(provider.base_url if provider else "")
        self._base_url.setPlaceholderText("https://openrouter.ai/api/v1")
        form.addRow("Name:", self._name)
        form.addRow("Base URL:", self._base_url)
        layout.addLayout(form)

        # При создании сервер требует хотя бы одну модель и один ключ;
        # при редактировании модели/ключи меняются кнопками на странице.
        self._models: QPlainTextEdit | None = None
        self._keys: QPlainTextEdit | None = None
        if provider is None:
            intro = QLabel(
                "A provider needs at least one model and one API key. Enter one per line.\n"
                "Generation calls go to {base_url}/chat/completions."
            )
            intro.setWordWrap(True)
            layout.addWidget(intro)
            extra = QFormLayout()
            self._models = QPlainTextEdit()
            self._models.setPlaceholderText("gpt-4o\ngpt-4o-mini")
            self._models.setMinimumHeight(80)
            self._keys = QPlainTextEdit()
            self._keys.setPlaceholderText("sk-...")
            self._keys.setMinimumHeight(80)
            extra.addRow("Models:", self._models)
            extra.addRow("API keys:", self._keys)
            layout.addLayout(extra)
        else:
            note = QLabel("Models and API keys are managed with the page buttons.")
            note.setWordWrap(True)
            layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept_validated)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_validated(self) -> None:
        if not self._name.text().strip():
            QMessageBox.warning(self, "Invalid input", "Name must not be empty.")
            return
        parsed = urlparse(self._base_url.text().strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            QMessageBox.warning(self, "Invalid input", "Base URL must be an http:// or https:// URL with a host.")
            return
        if self._models is not None:
            if not self.models():
                QMessageBox.warning(self, "Invalid input", "Enter at least one non-empty model name.")
                return
            if not self.keys():
                QMessageBox.warning(self, "Invalid input", "Enter at least one non-empty API key.")
                return
        self.accept()

    @staticmethod
    def _lines(edit: QPlainTextEdit) -> list[str]:
        result: list[str] = []
        for line in edit.toPlainText().splitlines():
            value = line.strip()
            if value and value not in result:
                result.append(value)
        return result

    def models(self) -> list[str]:
        return self._lines(self._models) if self._models is not None else []

    def keys(self) -> list[str]:
        return self._lines(self._keys) if self._keys is not None else []

    def payload(self) -> dict:
        data = {
            "name": self._name.text().strip(),
            "base_url": self._base_url.text().strip(),
        }
        if self._models is not None:
            data["models"] = self.models()
            data["api_keys"] = self.keys()
            data["is_active"] = True
        return data


class ModelsPage(QWidget):
    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.api: ApiClient | None = None
        self._settings = settings
        self._types: list[AITypeOut] = []
        self._providers: list[AICustomProviderOut] = []
        self._prompts: AiPromptsOut | None = None
        self._loading = False
        self._pending_requests = 0

        layout = QVBoxLayout(self)

        prompts_group = QGroupBox("Generation Prompts")
        prompts_layout = QVBoxLayout(prompts_group)
        editors_row = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.addWidget(QLabel("Title prompt:"))
        self._title_prompt = QPlainTextEdit()
        self._title_prompt.setPlaceholderText("Prompt used to generate the video title")
        self._title_prompt.setMinimumHeight(110)
        self._title_prompt.setMaximumHeight(170)
        title_col.addWidget(self._title_prompt)
        desc_col = QVBoxLayout()
        desc_col.addWidget(QLabel("Description prompt:"))
        self._description_prompt = QPlainTextEdit()
        self._description_prompt.setPlaceholderText("Prompt used to generate the video description")
        self._description_prompt.setMinimumHeight(110)
        self._description_prompt.setMaximumHeight(170)
        desc_col.addWidget(self._description_prompt)
        editors_row.addLayout(title_col, 1)
        editors_row.addLayout(desc_col, 1)
        prompts_layout.addLayout(editors_row)
        prompts_row = QHBoxLayout()
        self._prompts_status = QLabel("")
        prompts_row.addWidget(self._prompts_status)
        prompts_row.addStretch()
        self._save_prompts_btn = QPushButton("Save Prompts")
        prompts_row.addWidget(self._save_prompts_btn)
        prompts_layout.addLayout(prompts_row)
        layout.addWidget(prompts_group)

        types_group = QGroupBox("API Types & Providers")
        types_layout = QVBoxLayout(types_group)
        self._tree = QTreeWidget()
        self._tree.setColumnCount(2)
        self._tree.setHeaderLabels(["Item", "Details"])
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self._tree.setColumnWidth(0, 420)
        self._tree.header().setStretchLastSection(True)
        self._tree.itemSelectionChanged.connect(self._update_buttons)
        types_layout.addWidget(self._tree)

        list_buttons = QHBoxLayout()
        self._add_model_btn = QPushButton("Add Model")
        self._remove_model_btn = QPushButton("Remove Model")
        self._add_key_btn = QPushButton("Add Key")
        self._remove_key_btn = QPushButton("Remove Key")
        self._replace_keys_btn = QPushButton("Replace Keys")
        for button in (
            self._add_model_btn,
            self._remove_model_btn,
            self._add_key_btn,
            self._remove_key_btn,
            self._replace_keys_btn,
        ):
            list_buttons.addWidget(button)
        types_layout.addLayout(list_buttons)

        item_buttons = QHBoxLayout()
        self._add_provider_btn = QPushButton("Add Provider")
        self._edit_provider_btn = QPushButton("Edit Provider")
        self._toggle_active_btn = QPushButton("Toggle Active")
        self._delete_settings_btn = QPushButton("Delete Settings")
        self._refresh = QPushButton("Refresh")
        for button in (
            self._add_provider_btn,
            self._edit_provider_btn,
            self._toggle_active_btn,
            self._delete_settings_btn,
        ):
            item_buttons.addWidget(button)
        item_buttons.addStretch()
        item_buttons.addWidget(self._refresh)
        types_layout.addLayout(item_buttons)
        layout.addWidget(types_group, 1)

        self._save_prompts_btn.clicked.connect(self._save_prompts)
        self._add_model_btn.clicked.connect(self._add_model)
        self._remove_model_btn.clicked.connect(self._remove_model)
        self._add_key_btn.clicked.connect(self._add_key)
        self._remove_key_btn.clicked.connect(self._remove_key)
        self._replace_keys_btn.clicked.connect(self._replace_keys)
        self._add_provider_btn.clicked.connect(self._add_provider)
        self._edit_provider_btn.clicked.connect(self._edit_provider)
        self._toggle_active_btn.clicked.connect(self._toggle_active)
        self._delete_settings_btn.clicked.connect(self._delete_settings)
        self._refresh.clicked.connect(self.refresh)

        self._update_buttons()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_api(self, api: ApiClient) -> None:
        self.api = api

    def refresh(self) -> None:
        if not self.api or self._loading:
            return
        self._pending_requests = 3
        self._set_loading(True)
        run_api(self.api, self.api.get_ai_types, self._on_types_loaded, self._on_refresh_error, parent=self)
        run_api(self.api, self.api.get_ai_providers, self._on_providers_loaded, self._on_refresh_error, parent=self)
        run_api(self.api, self.api.get_ai_prompts, self._on_prompts_loaded, self._on_refresh_error, parent=self)

    def set_ai_types(self, types: list[AITypeOut]) -> None:
        self._types = list(types)
        self._reconcile_vault()
        self._render()

    def update_ai_type(self, ai_type: AITypeOut) -> None:
        for index, current in enumerate(self._types):
            if current.api_type == ai_type.api_type:
                if current == ai_type:
                    return
                self._types[index] = ai_type
                break
        else:
            self._types.append(ai_type)
        self._reconcile_vault()
        self._render()

    def on_ai_type_deleted(self, api_type: str) -> None:
        for index, current in enumerate(self._types):
            if current.api_type == api_type:
                self._types[index] = AITypeOut(api_type=api_type)
                break
        else:
            return
        self._reconcile_vault()
        self._render()

    def set_ai_providers(self, providers: list[AICustomProviderOut]) -> None:
        self._providers = list(providers)
        self._reconcile_vault()
        self._render()

    def update_ai_provider(self, provider: AICustomProviderOut) -> None:
        for index, current in enumerate(self._providers):
            if current.id == provider.id:
                if current == provider:
                    return
                self._providers[index] = provider
                break
        else:
            self._providers.append(provider)
        self._reconcile_vault()
        self._render()

    def remove_ai_provider(self, provider_id) -> None:
        pid = str(provider_id)
        if not any(str(p.id) == pid for p in self._providers):
            return
        self._providers = [p for p in self._providers if str(p.id) != pid]
        self._reconcile_vault()
        self._render()

    def set_prompts(self, prompts: AiPromptsOut) -> None:
        self._set_prompts(prompts)

    def update_prompts(self, title_prompt: str, description_prompt: str) -> None:
        self._set_prompts(AiPromptsOut(title_prompt=title_prompt, description_prompt=description_prompt))

    # ------------------------------------------------------------------
    # Prompts
    # ------------------------------------------------------------------
    def _set_prompts(self, prompts: AiPromptsOut) -> None:
        self._prompts = prompts
        for edit, value in (
            (self._title_prompt, prompts.title_prompt),
            (self._description_prompt, prompts.description_prompt),
        ):
            if edit.document().isModified():
                continue
            edit.setPlainText(value)
            edit.document().setModified(False)

    def _save_prompts(self) -> None:
        if not self.api or self._loading:
            return
        title = self._title_prompt.toPlainText().strip()
        description = self._description_prompt.toPlainText().strip()
        for name, value in (("Title prompt", title), ("Description prompt", description)):
            if not value:
                QMessageBox.warning(self, "Invalid prompt", f"{name} must not be empty.")
                return
            if len(value) > MAX_PROMPT_LENGTH:
                QMessageBox.warning(self, "Invalid prompt", f"{name} must be at most {MAX_PROMPT_LENGTH} characters.")
                return
        self._set_loading(True)
        run_api(
            self.api,
            self.api.update_ai_prompts,
            self._on_prompts_saved,
            self._on_mutate_error,
            {"title_prompt": title, "description_prompt": description},
            parent=self,
        )

    def _on_prompts_saved(self, prompts: AiPromptsOut) -> None:
        self._set_loading(False)
        self._prompts = prompts
        self._title_prompt.setPlainText(prompts.title_prompt)
        self._description_prompt.setPlainText(prompts.description_prompt)
        self._title_prompt.document().setModified(False)
        self._description_prompt.document().setModified(False)
        self._prompts_status.setText("Saved")

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------
    def _on_types_loaded(self, types: list[AITypeOut]) -> None:
        self._types = list(types)
        self._reconcile_vault()
        self._render()
        self._request_done()

    def _on_providers_loaded(self, providers: list[AICustomProviderOut]) -> None:
        self._providers = list(providers)
        self._reconcile_vault()
        self._render()
        self._request_done()

    def _on_prompts_loaded(self, prompts: AiPromptsOut) -> None:
        self._set_prompts(prompts)
        self._request_done()

    def _on_refresh_error(self, message: str) -> None:
        self._request_done(message)

    def _request_done(self, error: str | None = None) -> None:
        self._pending_requests = max(0, self._pending_requests - 1)
        if self._pending_requests == 0:
            self._set_loading(False)
        if error:
            QMessageBox.critical(self, "Models Error", error)

    def _set_loading(self, loading: bool) -> None:
        self._loading = loading
        self._refresh.setEnabled(not loading)
        self._refresh.setText("Refreshing..." if loading else "Refresh")
        self._save_prompts_btn.setEnabled(not loading)
        self._update_buttons()

    # ------------------------------------------------------------------
    # Vault (full key values known to this client)
    # ------------------------------------------------------------------
    @staticmethod
    def _match_full_keys(owner, full_keys: list[str]) -> list[str | None]:
        # Каждому серверному ключу (в его порядке) подбираем полный ключ из
        # локального хранилища по маске; один локальный ключ используется
        # не более одного раза.
        available = list(full_keys)
        matched: list[str | None] = []
        for server_key in owner.api_keys:
            found: str | None = None
            for index, candidate in enumerate(available):
                if mask_api_key(candidate) == server_key.key:
                    found = available.pop(index)
                    break
            matched.append(found)
        return matched

    @staticmethod
    def _vault_key(kind: str, owner) -> str:
        return owner.api_type if kind == "type" else provider_vault_key(owner.id)

    def _reconcile_vault(self) -> None:
        vault = self._settings.ai_key_vault
        changed = False
        containers = [("type", t) for t in self._types] + [("provider", p) for p in self._providers]
        for kind, owner in containers:
            vkey = self._vault_key(kind, owner)
            stored = vault.get(vkey)
            if stored is None:
                continue
            matched = self._match_full_keys(owner, stored)
            keep = [full for full in matched if full is not None]
            if keep != stored:
                if keep:
                    vault[vkey] = keep
                else:
                    del vault[vkey]
                changed = True
        known = {self._vault_key(kind, owner) for kind, owner in containers}
        for vkey in list(vault):
            if vkey not in known:
                del vault[vkey]
                changed = True
        if changed:
            self._settings.save()

    def _known_full_keys(self, owner, vkey: str) -> tuple[list[str], list[str], list[str | None]]:
        matched = self._match_full_keys(owner, self._settings.ai_key_vault.get(vkey, []))
        known = [full for full in matched if full is not None]
        external = [
            server_key.key
            for server_key, full in zip(owner.api_keys, matched)
            if full is None
        ]
        return known, external, matched

    # ------------------------------------------------------------------
    # Tree rendering
    # ------------------------------------------------------------------
    def _append_header(self, text: str) -> None:
        item = QTreeWidgetItem([text, ""])
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        self._tree.addTopLevelItem(item)

    def _render(self) -> None:
        selected_info = self._selected_info()
        expanded = self._expanded_tops()
        self._tree.clear()

        self._append_header("API Types")
        for ai_type in self._types:
            matched = self._match_full_keys(ai_type, self._settings.ai_key_vault.get(ai_type.api_type, []))
            type_item = QTreeWidgetItem([ai_type.api_type, "Active" if ai_type.is_active else "Inactive"])
            type_item.setData(0, Qt.ItemDataRole.UserRole, ("type", ai_type.api_type))
            type_item.setForeground(
                1, QColor("#2e7d32") if ai_type.is_active else QColor("#9e9e9e")
            )
            self._tree.addTopLevelItem(type_item)

            models_item = QTreeWidgetItem([f"Models ({len(ai_type.models)})", ""])
            models_item.setData(0, Qt.ItemDataRole.UserRole, ("models", ai_type.api_type))
            type_item.addChild(models_item)
            for name in ai_type.models:
                model_item = QTreeWidgetItem([name, ""])
                model_item.setData(0, Qt.ItemDataRole.UserRole, ("model", ai_type.api_type, name))
                models_item.addChild(model_item)

            keys_item = QTreeWidgetItem([f"API Keys ({len(ai_type.api_keys)})", ""])
            keys_item.setData(0, Qt.ItemDataRole.UserRole, ("keys", ai_type.api_type))
            type_item.addChild(keys_item)
            for server_key, full in zip(ai_type.api_keys, matched):
                key_item = self._key_item(server_key, full)
                key_item.setData(0, Qt.ItemDataRole.UserRole, ("key", ai_type.api_type, str(server_key.id)))
                keys_item.addChild(key_item)

            models_item.setExpanded(True)
            keys_item.setExpanded(True)
            if expanded:
                type_item.setExpanded(ai_type.api_type in expanded)
            else:
                type_item.setExpanded(True)

        self._append_header("Custom Providers (OpenAI-compatible)")
        for provider in self._providers:
            pid = str(provider.id)
            vkey = provider_vault_key(provider.id)
            matched = self._match_full_keys(provider, self._settings.ai_key_vault.get(vkey, []))
            label = provider.name if provider.is_active else f"{provider.name} [inactive]"
            provider_item = QTreeWidgetItem([label, provider.base_url])
            provider_item.setData(0, Qt.ItemDataRole.UserRole, ("provider", pid))
            provider_item.setForeground(
                1, QColor("#2e7d32") if provider.is_active else QColor("#9e9e9e")
            )
            provider_item.setToolTip(
                0,
                f"{'Active' if provider.is_active else 'Inactive'}\n"
                f"{provider.base_url}/chat/completions",
            )
            self._tree.addTopLevelItem(provider_item)

            models_item = QTreeWidgetItem([f"Models ({len(provider.models)})", ""])
            models_item.setData(0, Qt.ItemDataRole.UserRole, ("provider_models", pid))
            provider_item.addChild(models_item)
            for name in provider.models:
                model_item = QTreeWidgetItem([name, ""])
                model_item.setData(0, Qt.ItemDataRole.UserRole, ("provider_model", pid, name))
                models_item.addChild(model_item)

            keys_item = QTreeWidgetItem([f"API Keys ({len(provider.api_keys)})", ""])
            keys_item.setData(0, Qt.ItemDataRole.UserRole, ("provider_keys", pid))
            provider_item.addChild(keys_item)
            for server_key, full in zip(provider.api_keys, matched):
                key_item = self._key_item(server_key, full)
                key_item.setData(0, Qt.ItemDataRole.UserRole, ("provider_key", pid, str(server_key.id)))
                keys_item.addChild(key_item)

            models_item.setExpanded(True)
            keys_item.setExpanded(True)
            if expanded:
                provider_item.setExpanded(pid in expanded)
            else:
                provider_item.setExpanded(True)

        item = self._find_item(selected_info) if selected_info else None
        if item is None:
            for index in range(self._tree.topLevelItemCount()):
                candidate = self._tree.topLevelItem(index)
                if candidate.data(0, Qt.ItemDataRole.UserRole) is not None:
                    item = candidate
                    break
        if item is not None:
            self._tree.scrollToItem(item)
            self._tree.setCurrentItem(item)
        self._update_buttons()

    @staticmethod
    def _key_item(server_key, full: str | None) -> QTreeWidgetItem:
        label, color = KEY_STATUS_LABELS.get(server_key.status, KEY_STATUS_LABELS["unknown"])
        key_item = QTreeWidgetItem([server_key.key, label])
        key_item.setForeground(1, color)
        tooltip = f"Status: {label}"
        if server_key.last_checked_at:
            tooltip += f"\nLast checked: {server_key.last_checked_at.astimezone().strftime('%Y-%m-%d %H:%M')}"
        if server_key.last_error:
            tooltip += f"\nLast error: {server_key.last_error}"
        if full is None:
            tooltip += "\nAdded outside this client — the full key value is unknown."
        key_item.setToolTip(0, tooltip)
        return key_item

    def _expanded_tops(self) -> set[str]:
        expanded: set[str] = set()
        for index in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(index)
            info = item.data(0, Qt.ItemDataRole.UserRole)
            if info and item.isExpanded():
                expanded.add(info[1])
        return expanded

    def _find_item(self, info) -> QTreeWidgetItem | None:
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            if top.data(0, Qt.ItemDataRole.UserRole) == info:
                return top
            for j in range(top.childCount()):
                child = top.child(j)
                if child.data(0, Qt.ItemDataRole.UserRole) == info:
                    return child
                for k in range(child.childCount()):
                    grandchild = child.child(k)
                    if grandchild.data(0, Qt.ItemDataRole.UserRole) == info:
                        return grandchild
        return None

    def _selected_info(self) -> tuple | None:
        items = self._tree.selectedItems()
        if not items:
            return None
        return items[0].data(0, Qt.ItemDataRole.UserRole)

    def _selected_container(self) -> tuple[str, object] | None:
        """Выбранный контейнер ('type', AITypeOut) или ('provider', ...)."""
        info = self._selected_info()
        if not info:
            return None
        kind = info[0]
        if kind in TYPE_KINDS:
            ai_type = next((t for t in self._types if t.api_type == info[1]), None)
            return ("type", ai_type) if ai_type else None
        if kind in PROVIDER_KINDS:
            provider = next((p for p in self._providers if str(p.id) == info[1]), None)
            return ("provider", provider) if provider else None
        return None

    @staticmethod
    def _container_title(kind: str, owner) -> str:
        return owner.api_type if kind == "type" else owner.name

    def _update_buttons(self) -> None:
        info = self._selected_info()
        kind = info[0] if info else None
        container = self._selected_container()
        has_container = container is not None
        self._add_model_btn.setEnabled(has_container and not self._loading)
        self._remove_model_btn.setEnabled(kind in ("model", "provider_model") and not self._loading)
        self._add_key_btn.setEnabled(has_container and not self._loading)
        self._remove_key_btn.setEnabled(kind in ("key", "provider_key") and not self._loading)
        self._replace_keys_btn.setEnabled(has_container and not self._loading)
        self._add_provider_btn.setEnabled(not self._loading)
        self._edit_provider_btn.setEnabled(kind == "provider" and not self._loading)
        self._toggle_active_btn.setEnabled(kind in ("type", "provider") and not self._loading)
        self._delete_settings_btn.setEnabled(kind in ("type", "provider") and not self._loading)

    # ------------------------------------------------------------------
    # Container updates (types and providers share the flow)
    # ------------------------------------------------------------------
    def _submit_container_update(self, kind: str, owner, payload: dict, on_result=None) -> None:
        if kind == "type":
            run_api(
                self.api,
                self.api.update_ai_type,
                on_result or self._on_type_saved,
                self._on_mutate_error,
                owner.api_type,
                payload,
                parent=self,
            )
        else:
            run_api(
                self.api,
                self.api.update_ai_provider,
                on_result or self._on_provider_saved,
                self._on_mutate_error,
                owner.id,
                payload,
                parent=self,
            )

    def _on_type_saved(self, saved: AITypeOut) -> None:
        self._set_loading(False)
        self.update_ai_type(saved)

    def _on_provider_saved(self, saved: AICustomProviderOut) -> None:
        self._set_loading(False)
        self.update_ai_provider(saved)

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------
    def _add_model(self) -> None:
        container = self._selected_container()
        if not self.api or not container or self._loading:
            return
        kind, owner = container
        title = self._container_title(kind, owner)
        name, ok = QInputDialog.getText(self, "Add Model", f"Model name for '{title}':")
        if not ok:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "Invalid model", "Model name must not be empty.")
            return
        if name in owner.models:
            QMessageBox.information(self, "Add Model", f"Model '{name}' already exists for '{title}'.")
            return
        self._set_loading(True)
        self._submit_container_update(kind, owner, {"models": owner.models + [name]})

    def _remove_model(self) -> None:
        info = self._selected_info()
        container = self._selected_container()
        if not self.api or not info or info[0] not in ("model", "provider_model") or not container or self._loading:
            return
        kind, owner = container
        name = info[2]
        if len(owner.models) <= 1:
            QMessageBox.warning(
                self,
                "Remove Model",
                "At least one model must remain.\n"
                "Use 'Delete Settings' to remove the whole configuration.",
            )
            return
        if QMessageBox.question(self, "Confirm", f"Remove model '{name}' from '{self._container_title(kind, owner)}'?") != QMessageBox.Yes:
            return
        models = [model for model in owner.models if model != name]
        self._set_loading(True)
        self._submit_container_update(kind, owner, {"models": models})

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------
    def _add_key(self) -> None:
        container = self._selected_container()
        if not self.api or not container or self._loading:
            return
        kind, owner = container
        vkey = self._vault_key(kind, owner)
        dialog = ApiKeysDialog(self._container_title(kind, owner), replace=False, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        new_keys = dialog.keys()
        if not new_keys:
            return
        known, external, _ = self._known_full_keys(owner, vkey)
        server_masks = {server_key.key for server_key in owner.api_keys}
        duplicates = [key for key in new_keys if mask_api_key(key) in server_masks]
        if duplicates:
            QMessageBox.information(
                self,
                "Add Keys",
                "These keys are already saved:\n" + "\n".join(mask_api_key(key) for key in duplicates),
            )
            new_keys = [key for key in new_keys if mask_api_key(key) not in server_masks]
            if not new_keys:
                return
        if external:
            reply = QMessageBox.question(
                self,
                "Keys added outside this client",
                "The server has keys whose full values this client does not know:\n"
                + "\n".join(external)
                + "\n\nSaving the new key list removes them. Continue?",
            )
            if reply != QMessageBox.Yes:
                return
        self._submit_keys(kind, owner, known + new_keys)

    def _remove_key(self) -> None:
        info = self._selected_info()
        container = self._selected_container()
        if not self.api or not info or info[0] not in ("key", "provider_key") or not container or self._loading:
            return
        kind, owner = container
        key_id = info[2]
        server_key = next((item for item in owner.api_keys if str(item.id) == key_id), None)
        if server_key is None:
            return
        vkey = self._vault_key(kind, owner)
        known, external, matched = self._known_full_keys(owner, vkey)
        index = next(i for i, item in enumerate(owner.api_keys) if str(item.id) == key_id)
        full = matched[index]
        if full is None:
            if not known:
                QMessageBox.information(
                    self,
                    "Remove Key",
                    "The full value of this key is unknown to this client, and no other "
                    "stored key is known in full.\n"
                    "Use 'Replace Keys' to paste the complete list of keys you want to keep.",
                )
                return
            message = (
                f"The full value of '{server_key.key}' is unknown to this client (added elsewhere).\n"
                f"Removing it re-submits only locally known keys, so all {len(external)} "
                "unknown key(s) will be removed:\n"
                + "\n".join(external)
                + "\n\nContinue?"
            )
            if QMessageBox.question(self, "Remove Key", message) != QMessageBox.Yes:
                return
            new_list = known
        else:
            new_list = [key for key in known if key != full]
            if not new_list:
                QMessageBox.warning(
                    self,
                    "Remove Key",
                    "At least one key must remain.\n"
                    "Use 'Delete Settings' to remove the whole configuration.",
                )
                return
            message = f"Remove key '{server_key.key}' from '{self._container_title(kind, owner)}'?"
            if external:
                message += (
                    f"\n\n{len(external)} key(s) added outside this client will also be removed:\n"
                    + "\n".join(external)
                )
            if QMessageBox.question(self, "Remove Key", message) != QMessageBox.Yes:
                return
        self._submit_keys(kind, owner, new_list)

    def _replace_keys(self) -> None:
        container = self._selected_container()
        if not self.api or not container or self._loading:
            return
        kind, owner = container
        dialog = ApiKeysDialog(self._container_title(kind, owner), replace=True, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        full_list = dialog.keys()
        if not full_list:
            return
        if (
            QMessageBox.question(
                self,
                "Confirm",
                f"Replace all keys of '{self._container_title(kind, owner)}' with {len(full_list)} key(s)?",
            )
            != QMessageBox.Yes
        ):
            return
        self._submit_keys(kind, owner, full_list)

    def _submit_keys(self, kind: str, owner, full_keys: list[str]) -> None:
        if not self.api:
            return
        vkey = self._vault_key(kind, owner)
        self._set_loading(True)
        self._submit_container_update(
            kind,
            owner,
            {"api_keys": full_keys},
            on_result=lambda saved: self._on_keys_saved(vkey, full_keys, saved),
        )

    def _on_keys_saved(self, vkey: str, submitted: list[str], saved) -> None:
        # Восстанавливаем локальное хранилище из ответа: сопоставляем
        # отправленные полные ключи с серверными записями по маске.
        matched = self._match_full_keys(saved, submitted)
        vault_keys = [full for full in matched if full is not None]
        if len(vault_keys) != len(submitted):
            vault_keys = list(submitted)
        if vault_keys:
            self._settings.ai_key_vault[vkey] = vault_keys
        else:
            self._settings.ai_key_vault.pop(vkey, None)
        self._settings.save()
        if isinstance(saved, AITypeOut):
            self._on_type_saved(saved)
        else:
            self._on_provider_saved(saved)

    # ------------------------------------------------------------------
    # Providers (create / edit)
    # ------------------------------------------------------------------
    def _add_provider(self) -> None:
        if not self.api or self._loading:
            return
        dialog = ProviderDialog(parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.payload()
        submitted_keys = payload.get("api_keys", [])
        self._set_loading(True)
        run_api(
            self.api,
            self.api.create_ai_provider,
            lambda saved: self._on_provider_created(saved, submitted_keys),
            self._on_mutate_error,
            payload,
            parent=self,
        )

    def _on_provider_created(self, saved: AICustomProviderOut, submitted_keys: list[str]) -> None:
        if submitted_keys:
            vkey = provider_vault_key(saved.id)
            matched = self._match_full_keys(saved, submitted_keys)
            vault_keys = [full for full in matched if full is not None]
            if len(vault_keys) != len(submitted_keys):
                vault_keys = list(submitted_keys)
            if vault_keys:
                self._settings.ai_key_vault[vkey] = vault_keys
                self._settings.save()
        self._on_provider_saved(saved)

    def _edit_provider(self) -> None:
        info = self._selected_info()
        container = self._selected_container()
        if not self.api or not info or info[0] != "provider" or not container or self._loading:
            return
        kind, owner = container
        dialog = ProviderDialog(provider=owner, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._set_loading(True)
        self._submit_container_update(kind, owner, dialog.payload())

    # ------------------------------------------------------------------
    # Type/provider-level actions
    # ------------------------------------------------------------------
    def _toggle_active(self) -> None:
        info = self._selected_info()
        container = self._selected_container()
        if not self.api or not info or info[0] not in ("type", "provider") or not container or self._loading:
            return
        kind, owner = container
        self._set_loading(True)
        self._submit_container_update(kind, owner, {"is_active": not owner.is_active})

    def _delete_settings(self) -> None:
        info = self._selected_info()
        container = self._selected_container()
        if not self.api or not info or info[0] not in ("type", "provider") or not container or self._loading:
            return
        kind, owner = container
        if kind == "type":
            message = (
                f"Delete all settings (models and API keys) for '{owner.api_type}'?\n"
                "Channels using it fall back to their templates."
            )
            if QMessageBox.question(self, "Confirm", message) != QMessageBox.Yes:
                return
            self._set_loading(True)
            run_api(
                self.api,
                self.api.delete_ai_type,
                lambda _: self._on_type_deleted(owner.api_type),
                self._on_mutate_error,
                owner.api_type,
                parent=self,
            )
        else:
            message = (
                f"Delete provider '{owner.name}' ({owner.base_url}) with all its models and API keys?\n"
                "Channels using it fall back to their templates."
            )
            if QMessageBox.question(self, "Confirm", message) != QMessageBox.Yes:
                return
            self._set_loading(True)
            run_api(
                self.api,
                self.api.delete_ai_provider,
                lambda _: self._on_provider_deleted(str(owner.id)),
                self._on_mutate_error,
                owner.id,
                parent=self,
            )

    def _on_type_deleted(self, api_type: str) -> None:
        self._set_loading(False)
        self._settings.ai_key_vault.pop(api_type, None)
        self._settings.save()
        self.on_ai_type_deleted(api_type)

    def _on_provider_deleted(self, provider_id: str) -> None:
        self._set_loading(False)
        self._settings.ai_key_vault.pop(provider_vault_key(provider_id), None)
        self._settings.save()
        self.remove_ai_provider(provider_id)

    def _on_mutate_error(self, message: str) -> None:
        self._set_loading(False)
        QMessageBox.critical(self, "Models Error", message)
