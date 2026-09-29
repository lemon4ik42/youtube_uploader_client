from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import AliasChoices, BaseModel, Field, field_validator


class ProjectOut(BaseModel):
    id: UUID
    name: str
    client_id: str
    redirect_uri: str
    scope: str = "https://www.googleapis.com/auth/youtube"
    token_uri: str = "https://oauth2.googleapis.com/token"
    auth_uri: str = "https://accounts.google.com/o/oauth2/auth"
    api_upload: str = "https://www.googleapis.com/upload/youtube/v3/videos"
    quota_used: int = 0
    quota_reset_at: Optional[datetime] = None
    is_active: bool = True
    created_at: datetime


class ChannelTagOut(BaseModel):
    id: UUID
    name: str


class ChannelOut(BaseModel):
    id: UUID
    youtube_channel_id: str
    title: Optional[str] = None
    youtube_title: Optional[str] = None
    handle: Optional[str] = None
    subscriber_count: Optional[int] = None
    view_count: Optional[int] = None
    metadata_updated_at: Optional[datetime] = None
    project_id: Optional[UUID] = None
    folder_path: Optional[str] = None
    title_template: str = "{filename}"
    description_template: str = ""
    privacy_status: Literal["public", "unlisted", "private"] = "public"
    default_category_id: Optional[str] = None
    auto_upload_enabled: bool = True
    status: Literal["active", "paused", "error", "deleted"] = "active"
    session_status: Literal["active", "expired", "reauth_required"] = "active"
    session_status_changed_at: Optional[datetime] = None
    paused_until: Optional[datetime] = None
    pause_reason: Optional[str] = None
    error_message: Optional[str] = None
    last_upload_at: Optional[datetime] = None
    thumbnail_url: Optional[str] = None
    upload_limit_enabled: bool = False
    upload_limit_count: int = 0
    upload_limit_period_hours: int = 0
    min_batch_size: int = 1
    max_batch_size: int = 200
    inter_upload_delay_seconds: int = 2
    shuffle_upload_order: bool = False
    # When enabled, the server asynchronously checks each uploaded video for
    # an all-country copyright block, deletes it from YouTube, and excludes
    # the file hash from later uploads.
    skip_blocked_videos: bool = False
    proxy_id: Optional[UUID] = None
    proxy_auto_switch: bool = False
    proxy_recovery_pending: bool = False
    ai_model: Optional[str] = None
    ai_generate_description: bool = False
    ai_use_cached: bool = False
    tags: list[ChannelTagOut] = Field(default_factory=list)
    # Client-side operation state. It is deliberately excluded from API payloads.
    loading_operations: set[str] = Field(default_factory=set, exclude=True)
    created_at: datetime
    updated_at: datetime


class ChannelSessionStatusOut(BaseModel):
    channel_id: UUID
    session_status: Literal["active", "expired", "reauth_required"]
    session_status_changed_at: Optional[datetime] = None


class ChannelBulkSettingsResult(BaseModel):
    updated: list[ChannelOut] = Field(default_factory=list)
    not_found: list[UUID] = Field(default_factory=list)


class UploadLimitOut(BaseModel):
    enabled: bool = False
    count: int = 0
    period_hours: int = 0
    uploads_in_current_period: int = 0
    period_started_at: Optional[datetime] = None
    period_ends_at: Optional[datetime] = None
    is_limited: bool = False
    remaining: int = 0
    reset_at: Optional[datetime] = None


class ChannelAvatarOut(BaseModel):
    channel_id: UUID | None = None
    thumbnail_url: str


class AiApiKeyOut(BaseModel):
    id: UUID
    key: str
    status: Literal["working", "not_working", "quota_exceeded", "unknown"] = "unknown"
    last_checked_at: Optional[datetime] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AITypeOut(BaseModel):
    api_type: str
    models: list[str] = Field(default_factory=list)
    api_keys: list[AiApiKeyOut] = Field(default_factory=list)
    is_active: bool = False


class AICustomProviderOut(BaseModel):
    id: UUID
    name: str
    base_url: str
    models: list[str] = Field(default_factory=list)
    api_keys: list[AiApiKeyOut] = Field(default_factory=list)
    is_active: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AiPromptsOut(BaseModel):
    title_prompt: str = ""
    description_prompt: str = ""


class ProxyOut(BaseModel):
    id: UUID
    url: str
    is_active: bool = True
    assigned_channels: int = 0
    last_checked_at: Optional[datetime] = None
    last_check_ok: Optional[bool] = None
    status: Literal["unknown", "working", "failed"] = "unknown"
    failure_count: int = 0
    # POST /proxies/check возвращает сокращённые объекты (id/url/status),
    # поэтому метки времени необязательны.
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ProxyImportResult(BaseModel):
    received: int
    added: int
    duplicates: int


class ProxyCheckResult(BaseModel):
    checked: int
    working: int
    failed: int
    proxies: list[ProxyOut] = Field(default_factory=list)


class ChannelUploadedCount(BaseModel):
    total_uploaded: int
    uploads_in_current_period: int
    period_hours: int
    is_limited: bool
    remaining: int | None = None
    reset_at: Optional[datetime] = None


class FolderVideoOut(BaseModel):
    channel_id: UUID | None = None
    file_name: str
    size_bytes: int
    modified_at: Optional[datetime] = None


class UploadFileResult(BaseModel):
    file_name: str
    size_bytes: Optional[int] = None
    modified_at: Optional[datetime] = None
    skipped: bool = False
    reason: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None


class TaskOut(BaseModel):
    id: UUID
    batch_id: Optional[UUID] = None
    channel_id: UUID
    file_path: str
    file_hash: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    status: Literal[
        "pending",
        "session_created",
        "uploading",
        "retry",
        "completed",
        "failed",
        "cancelled",
    ] = "pending"
    youtube_video_id: Optional[str] = None
    progress_percent: int = 0
    attempt: int = 0
    error_message: Optional[str] = None
    retry_after: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class AuthStartResponse(BaseModel):
    state: str
    authorization_url: str
    project_id: UUID
    channel_id: Optional[UUID] = None
    proxy_settings: dict | None = None
    expires_at: Optional[datetime] = None
    authorization_timeout_seconds: float | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "authorization_timeout_seconds",
            "timeout_seconds",
            "auth_timeout",
            "expires_in_seconds",
            "expires_in",
            "timeout",
        ),
    )


class AuthCallbackPayload(BaseModel):
    state: str
    code: str
    tag_ids: list[UUID] | None = None


class DeleteFileResponse(BaseModel):
    ok: bool
    channel_id: UUID
    file_name: str
    task_id: Optional[str] = None

    @field_validator("task_id", mode="before")
    @classmethod
    def _task_id(cls, v):
        if v is None or v == "null" or v == "":
            return None
        return str(v)


class AckResponse(BaseModel):
    acknowledged: int


class ConnectionOpened(BaseModel):
    subscriber_id: UUID
    pending_count: int = 0
