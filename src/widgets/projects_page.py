from uuid import UUID

from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from uuid import UUID

from src.api_client import ApiClient
from src.api_worker import run_api
from src.models import ProjectOut


class ProjectsPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.api: ApiClient | None = None
        self._projects: list[ProjectOut] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._table = QTableView()
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        layout.addWidget(self._table)

        btn_layout = QHBoxLayout()
        self._add_btn = QPushButton("Add Project")
        self._edit_btn = QPushButton("Edit Project")
        self._delete_btn = QPushButton("Delete Project")
        self._refresh_btn = QPushButton("Refresh")
        btn_layout.addWidget(self._add_btn)
        btn_layout.addWidget(self._edit_btn)
        btn_layout.addWidget(self._delete_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self._refresh_btn)
        layout.addLayout(btn_layout)

        self._add_btn.clicked.connect(self._add_project)
        self._edit_btn.clicked.connect(self._edit_project)
        self._delete_btn.clicked.connect(self._delete_project)
        self._refresh_btn.clicked.connect(self.refresh)

        self._model = QStandardItemModel(0, 4)
        self._model.setHorizontalHeaderLabels(["Name", "Client ID", "Quota Used", "Active"])
        self._table.setModel(self._model)

    def set_api(self, api: ApiClient) -> None:
        self.api = api

    def set_projects(self, projects: list[ProjectOut]) -> None:
        self._projects = projects
        self._render()

    def refresh(self) -> None:
        if not self.api:
            return
        self._set_loading(True)
        run_api(self.api, self.api.get_projects, self._on_loaded, self._on_error, parent=self)

    def _on_loaded(self, projects: list[ProjectOut]) -> None:
        self._set_loading(False)
        self._projects = projects
        self._render()

    def _on_error(self, message: str) -> None:
        self._set_loading(False)
        # Silent on background refresh

    def _render(self) -> None:
        self._model.removeRows(0, self._model.rowCount())
        for p in self._projects:
            row = [
                QStandardItem(p.name),
                QStandardItem(p.client_id[:20] + "..."),
                QStandardItem(str(p.quota_used)),
                QStandardItem("Yes" if p.is_active else "No"),
            ]
            for item in row:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._model.appendRow(row)

    def _set_loading(self, loading: bool) -> None:
        self._refresh_btn.setEnabled(not loading)
        self._refresh_btn.setText("Refreshing..." if loading else "Refresh")
        self._add_btn.setEnabled(not loading)
        self._edit_btn.setEnabled(not loading)
        self._delete_btn.setEnabled(not loading)

    def _selected_project(self) -> ProjectOut | None:
        indexes = self._table.selectionModel().selectedRows()
        if not indexes:
            return None
        row = indexes[0].row()
        if 0 <= row < len(self._projects):
            return self._projects[row]
        return None

    def _add_project(self) -> None:
        if not self.api:
            return
        dlg = ProjectDialog(self)
        if dlg.exec():
            self._set_loading(True)
            run_api(
                self.api,
                self.api.create_project,
                lambda _: self.refresh(),
                self._on_mutate_error,
                dlg.payload(),
                parent=self,
            )

    def _edit_project(self) -> None:
        if not self.api:
            return
        proj = self._selected_project()
        if not proj:
            QMessageBox.information(self, "Info", "Select a project to edit.")
            return
        dlg = ProjectDialog(self, project=proj)
        if dlg.exec():
            self._set_loading(True)
            run_api(
                self.api,
                self.api.update_project,
                lambda _: self.refresh(),
                self._on_mutate_error,
                proj.id,
                dlg.payload(),
                parent=self,
            )

    def _delete_project(self) -> None:
        if not self.api:
            return
        proj = self._selected_project()
        if not proj:
            return
        reply = QMessageBox.question(self, "Confirm", f"Delete project '{proj.name}'?")
        if reply == QMessageBox.Yes:
            self._set_loading(True)
            run_api(
                self.api,
                self.api.delete_project,
                lambda _: self.refresh(),
                self._on_mutate_error,
                proj.id,
                parent=self,
            )

    def _on_mutate_error(self, message: str) -> None:
        self._set_loading(False)
        QMessageBox.critical(self, "Error", message)


class ProjectDialog(QMessageBox):
    """Simple form dialog embedded via QDialog would be cleaner, but using custom dialog."""

    def __init__(self, parent=None, project: ProjectOut | None = None) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox

        super().__init__(parent)
        self._project = project
        self.setWindowTitle("Edit Project" if project else "Add Project")

        # Build a custom dialog
        self._dlg = QDialog(parent)
        self._dlg.setWindowTitle(self.windowTitle())
        self._dlg.setMinimumWidth(400)
        layout = QVBoxLayout(self._dlg)
        form = QFormLayout()

        self._name = QLineEdit(project.name if project else "")
        self._client_id = QLineEdit(project.client_id if project else "")
        self._client_secret = QLineEdit("")
        self._client_secret.setEchoMode(QLineEdit.Password)
        self._client_secret.setPlaceholderText(
            "Leave blank to keep unchanged" if project else ""
        )
        self._redirect = QLineEdit(project.redirect_uri if project else "http://localhost:8080/callback")

        form.addRow("Name:", self._name)
        form.addRow("Client ID:", self._client_id)
        form.addRow("Client Secret:", self._client_secret)
        form.addRow("Redirect URI:", self._redirect)
        layout.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._dlg.accept)
        btns.rejected.connect(self._dlg.reject)
        layout.addWidget(btns)

    def exec(self) -> bool:
        return self._dlg.exec() == 1  # QDialog.Accepted

    def payload(self) -> dict:
        data = {
            "name": self._name.text().strip(),
            "client_id": self._client_id.text().strip(),
            "redirect_uri": self._redirect.text().strip(),
        }
        secret = self._client_secret.text().strip()
        if secret:
            data["client_secret"] = secret
        return data
