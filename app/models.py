import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

LinkMode = Literal["file", "api"]

VALID_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
FORBIDDEN_INJECT_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
        "proxy-connection",
        "proxy-authenticate",
        "proxy-authorization",
    }
)
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class LinkCreateRequest(BaseModel):
    target_url: HttpUrl
    expires_in: Optional[int] = Field(default=None, ge=0)
    custom_slug: Optional[str] = None
    filename: Optional[str] = None
    max_uses: Optional[int] = Field(default=None, ge=1)

    # Modo dinámico (API / MCP / polling)
    mode: LinkMode = "file"
    allowed_methods: Optional[list[str]] = None
    forward_subpaths: Optional[bool] = None
    inject_headers: Optional[dict[str, str]] = None
    cors_origins: Optional[list[str]] = None
    count_preflight: Optional[bool] = None

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
        if any(ch in value for ch in ('"', ";", "\r", "\n", "\0")):
            raise ValueError("filename no puede contener comillas dobles ni punto y coma")
        return value

    @field_validator("allowed_methods")
    @classmethod
    def validate_allowed_methods(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        normalized: list[str] = []
        for method in value:
            m = method.strip().upper()
            if m not in VALID_HTTP_METHODS:
                raise ValueError(f"Método HTTP no permitido: {method}")
            if m not in normalized:
                normalized.append(m)
        if not normalized:
            raise ValueError("allowed_methods no puede estar vacío")
        return normalized

    @field_validator("inject_headers")
    @classmethod
    def validate_inject_headers(cls, value: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
        if value is None:
            return None
        cleaned: dict[str, str] = {}
        seen_lower: set[str] = set()
        for raw_key, raw_val in value.items():
            key = raw_key.strip()
            if not key or not _HEADER_NAME_RE.match(key):
                raise ValueError(f"Nombre de header inválido: {raw_key!r}")
            lower_key = key.lower()
            if lower_key in FORBIDDEN_INJECT_HEADERS:
                raise ValueError(f"Header reservado no permitido en inject_headers: {key}")
            if lower_key in seen_lower:
                raise ValueError(f"Header duplicado en inject_headers: {key}")
            if any((ord(ch) < 32 and ch != "\t") or ord(ch) == 127 for ch in raw_val):
                raise ValueError(f"Valor de header contiene caracteres de control no permitidos: {key}")
            seen_lower.add(lower_key)
            cleaned[key] = raw_val
        return cleaned or None

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return None
        cleaned: list[str] = []
        for raw in value:
            origin = raw.strip().rstrip("/")
            if not origin:
                continue
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in origin):
                raise ValueError("cors_origins contiene caracteres de control no permitidos")
            if origin != "*" and not (origin.startswith("http://") or origin.startswith("https://")):
                raise ValueError(f"Origen CORS inválido: {raw!r} (debe ser '*' o iniciar con http:// o https://)")
            if origin not in cleaned:
                cleaned.append(origin)
        return cleaned or None

    @model_validator(mode="after")
    def validate_mode_options(self) -> "LinkCreateRequest":
        if self.mode == "file":
            api_only = [
                name
                for name, val in (
                    ("allowed_methods", self.allowed_methods),
                    ("forward_subpaths", self.forward_subpaths),
                    ("inject_headers", self.inject_headers),
                    ("cors_origins", self.cors_origins),
                    ("count_preflight", self.count_preflight),
                )
                if val is not None
            ]
            if api_only:
                raise ValueError(f"Las opciones {', '.join(api_only)} solo aplican cuando mode='api'")
        return self


class LinkCreateResponse(BaseModel):
    slug: str
    url: str
    expires_at: int
    mode: LinkMode = "file"


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
    mode: LinkMode = "file"
    allowed_methods: Optional[list[str]] = None
    forward_subpaths: Optional[bool] = None
    inject_headers: Optional[dict[str, str]] = None
    cors_origins: Optional[list[str]] = None
    count_preflight: Optional[bool] = None
