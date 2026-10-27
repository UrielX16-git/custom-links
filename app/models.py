from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator


class LinkCreateRequest(BaseModel):
    target_url: HttpUrl
    expires_in: Optional[int] = Field(default=None, ge=0)
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
        if value.startswith(".") or value.endswith(".") or ".." in value:
            raise ValueError("custom_slug no puede empezar ni terminar con punto, ni contener puntos consecutivos")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
        if any(ch not in allowed for ch in value):
            raise ValueError("custom_slug inválido (solo letras, números, guiones, guiones bajos y puntos)")
        return value

    @field_validator("filename")
    @classmethod
    def validate_filename(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip()
        if not value:
            return None
        # Prohibir caracteres de control y saltos de línea (prevención CRLF injection)
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("filename contiene caracteres de control o saltos de línea no permitidos")
        # Prohibir comillas dobles y punto y coma que rompen la cabecera HTTP
        if any(ch in value for ch in ('"', ';', '\r', '\n', '\0')):
            raise ValueError("filename no puede contener comillas dobles ni punto y coma")
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
