"""Consistent UTC serialization for SQLite timestamps."""
from datetime import datetime, timezone
from pydantic import BaseModel, field_validator


class UTCResponse(BaseModel):
    @field_validator("created_at", "completed_at", check_fields=False)
    @classmethod
    def utc_timestamp(cls, value: datetime | None):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
