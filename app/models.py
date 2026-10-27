from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class LinkCreateRequest(BaseModel):
    target_url: HttpUrl
    expires_in: int = Field(default=3600, ge=1)
    custom_slug: Optional[str] = None
    filename: Optional[str] = None
    max_uses: Optional[int] = Field(default=None, ge=1)

    @field_validator("custom_slug")
    @classmethod
    def validate_slug(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("custom_slug vacío")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
        if any(ch not in allowed for ch in value):
            raise ValueError("custom_slug inválido")
        return value


class LinkCreateResponse(BaseModel):
    slug: str
    url: str
    expires_at: int


class LinkInfoResponse(BaseModel):
    slug: str
    target_url: str
    expires_at: int
    revoked: bool
    filename: Optional[str]
    max_uses: Optional[int]
    use_count: int
    created_at: int
    last_access_at: Optional[int]
