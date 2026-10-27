from pydantic import AnyHttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "custom-links"
    api_token: str
    base_url: AnyHttpUrl
    db_path: str = "/data/links.db"
    default_expires_in: int = 3600
    max_expires_in: int = 60 * 60 * 24 * 30
    max_slug_len: int = 80

    # CORS
    cors_origins: str = "*"
    cors_allow_credentials: bool = True

    # Limpieza automática periódica
    auto_cleanup_enabled: bool = True
    cleanup_interval_seconds: int = 86400  # 24 horas por defecto

    @field_validator("base_url", mode="before")
    @classmethod
    def strip_base_url(cls, value: str) -> str:
        return str(value).rstrip("/")

    @property
    def cors_origins_list(self) -> list[str]:
        if not self.cors_origins:
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


settings = Settings()
