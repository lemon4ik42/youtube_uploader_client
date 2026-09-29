from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from src.models import TaskOut
from src.utils import format_local, task_sort_key


class TaskTableModel(QAbstractTableModel):
    COLUMNS = [
        ("File", lambda t: t.file_path.split("/")[-1].split("\\")[-1]),
        ("Status", lambda t: t.status),
        ("Progress", lambda t: f"{t.progress_percent}%"),
        ("Attempt", lambda t: str(t.attempt)),
        ("Error", lambda t: t.error_message or ""),
        ("Created", lambda t: format_local(t.created_at)),
    ]

    def __init__(self, max_tasks: int = 200, parent=None) -> None:
        super().__init__(parent)
        self._tasks: list[TaskOut] = []
        self._max_tasks = max_tasks

    def _trim_old(self) -> None:
        if len(self._tasks) <= self._max_tasks:
            return
        # Sort by updated_at descending, keep newest _max_tasks
        sorted_tasks = sorted(self._tasks, key=task_sort_key, reverse=True)
        keep_ids = {str(t.id) for t in sorted_tasks[: self._max_tasks]}
        to_remove = [i for i, t in enumerate(self._tasks) if str(t.id) not in keep_ids]
        for i in reversed(to_remove):
            self.beginRemoveRows(QModelIndex(), i, i)
            del self._tasks[i]
            self.endRemoveRows()

    def set_tasks(self, tasks: list[TaskOut]) -> None:
        self.beginResetModel()
        sorted_tasks = sorted(tasks, key=task_sort_key, reverse=True)
        self._tasks = sorted_tasks[: self._max_tasks]
        self.endResetModel()

    def update_task(self, task: TaskOut) -> None:
        for i, t in enumerate(self._tasks):
            if str(t.id) == str(task.id):
                self._tasks[i] = task
                self.dataChanged.emit(self.index(i, 0), self.index(i, len(self.COLUMNS) - 1))
                return
        # New task: insert at the TOP so the table stays "newest first";
        # appending put freshly created tasks below old ones.
        self.beginInsertRows(QModelIndex(), 0, 0)
        self._tasks.insert(0, task)
        self.endInsertRows()
        self._trim_old()

    def update_tasks(self, tasks: list[TaskOut]) -> None:
        """Batch update/insert tasks with minimal model notifications."""
        if not tasks:
            return
        existing_indices = {str(t.id): i for i, t in enumerate(self._tasks)}
        to_insert: list[TaskOut] = []
        updated_rows: list[int] = []
        for task in tasks:
            sid = str(task.id)
            idx = existing_indices.get(sid)
            if idx is not None:
                self._tasks[idx] = task
                updated_rows.append(idx)
            else:
                to_insert.append(task)
        if updated_rows:
            top = min(updated_rows)
            bottom = max(updated_rows)
            self.dataChanged.emit(self.index(top, 0), self.index(bottom, len(self.COLUMNS) - 1))
        if to_insert:
            # Insert the new block at the TOP (newest first), reversed so the
            # most recently created task of the block ends up topmost.
            block = list(reversed(to_insert))
            self.beginInsertRows(QModelIndex(), 0, len(block) - 1)
            self._tasks[0:0] = block
            self.endInsertRows()
        self._trim_old()

    def remove_task(self, task_id) -> None:
        sid = str(task_id)
        for i, t in enumerate(self._tasks):
            if str(t.id) == sid:
                print(f"[TaskTableModel] remove row={i} task_id={task_id}")
                self.beginRemoveRows(QModelIndex(), i, i)
                del self._tasks[i]
                self.endRemoveRows()
                return
        print(f"[TaskTableModel] remove NOT FOUND task_id={task_id}")

    def get_task(self, row: int) -> TaskOut | None:
        if 0 <= row < len(self._tasks):
            return self._tasks[row]
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self._tasks)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLUMNS[section][0]
        return None

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        task = self._tasks[row]
        if role == Qt.DisplayRole:
            return self.COLUMNS[col][1](task)
        if role == Qt.UserRole:
            return task
        if role == Qt.ForegroundRole and col == 1:
            colors = {
                "pending": QColor("#757575"),
                "session_created": QColor("#0288d1"),
                "uploading": QColor("#1565c0"),
                "retry": QColor("#ed6c02"),
                "completed": QColor("#2e7d32"),
                "failed": QColor("#d32f2f"),
                "cancelled": QColor("#757575"),
            }
            return colors.get(task.status)
        if role == Qt.ForegroundRole and col == 4 and task.error_message:
            return QColor("#d32f2f")
        return None
