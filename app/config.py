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

    @field_validator("base_url", mode="before")
    @classmethod
    def strip_base_url(cls, value: str) -> str:
        return str(value).rstrip("/")


settings = Settings()
