from __future__ import annotations

from datetime import datetime, timezone


def task_sort_key(task) -> datetime:
    """Return an offset-aware UTC datetime for sorting tasks.

    Tasks are ordered by creation time (newest first): the user expects
    "new tasks on top, old ones below". ``updated_at`` must not drive the
    order — an old task that just changed status would jump above newly
    created ones.
    """
    dt = task.created_at or task.updated_at
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_local(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Convert a UTC datetime to local timezone and format."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)
