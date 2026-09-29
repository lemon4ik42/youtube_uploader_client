from datetime import datetime, timezone

from src.utils import format_local


def tag_text(channel):
    return "  ".join(f"#{tag.name}" for tag in channel.tags) or "No tags"


def dashboard_tag_text(channel, chunk_size: int = 24):
    """Return tag text with invisible wrapping points for very long tag names."""
    if not channel.tags:
        return "No tags"
    def wrap(value: str) -> str:
        return "\u200b".join(value[i:i + chunk_size] for i in range(0, len(value), chunk_size))
    return "  ".join(f"#{wrap(tag.name)}" for tag in channel.tags)


def authorization_age(channel):
    created = channel.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - created).days)


def authorization_text(channel):
    return f"{format_local(channel.created_at)} · {authorization_age(channel)}d"


def age_color(channel):
    days = authorization_age(channel)
    return "#e3f2fd" if days < 7 else "#ede7f6" if days < 30 else "#e0f2f1"


def upload_color(count):
    return "#eeeeee" if count is None or count == 0 else "#e3f2fd" if count < 100 else "#e0f2f1"
