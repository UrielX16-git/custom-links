from urllib.parse import urlsplit

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

    # CORS (API de gestión)
    cors_origins: str = "*"
    cors_allow_credentials: bool = True

    # Limpieza automática periódica
    auto_cleanup_enabled: bool = True
    cleanup_interval_seconds: int = 86400  # 24 horas por defecto

    # Enlaces dinámicos (modo API / MCP / polling)
    dynamic_links_enabled: bool = False
    dynamic_allowed_hosts: str = ""
    dynamic_default_methods: str = "GET,POST,PUT,PATCH,DELETE,HEAD,OPTIONS"
    dynamic_timeout_seconds: float = 30.0
    dynamic_read_timeout_seconds: float = 0.0  # 0 = sin límite de lectura (útil para SSE / MCP / long-polling)
    dynamic_max_body_bytes: int = 10 * 1024 * 1024  # 10 MB (0 = sin límite)

    @field_validator("base_url", mode="before")
    @classmethod
    def strip_base_url(cls, value: str) -> str:
        return str(value).rstrip("/")

    @property
    def cors_origins_list(self) -> list[str]:
        if not self.cors_origins:
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def dynamic_allowed_hosts_list(self) -> list[str]:
        if not self.dynamic_allowed_hosts:
            return []
        return [h.strip().lower() for h in self.dynamic_allowed_hosts.split(",") if h.strip()]

    @property
    def dynamic_default_methods_list(self) -> list[str]:
        if not self.dynamic_default_methods:
            return ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
        methods: list[str] = []
        for m in self.dynamic_default_methods.split(","):
            clean = m.strip().upper()
            if clean and clean not in methods:
                methods.append(clean)
        return methods or ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

    def is_dynamic_host_allowed(self, target_url: str) -> bool:
        allowed = self.dynamic_allowed_hosts_list
        if not allowed:
            return True

        parsed = urlsplit(target_url)
        target_host = (parsed.hostname or "").lower()
        target_port = parsed.port
        if not target_host:
            return False

        for rule in allowed:
            if rule.startswith("*."):
                # Soporta *.local o *.local:8080
                rule_host, _, rule_port_str = rule[2:].partition(":")
                if rule_port_str:
                    try:
                        rule_port = int(rule_port_str)
                    except ValueError:
                        continue
                    if target_port != rule_port:
                        continue
                if target_host == rule_host or target_host.endswith(f".{rule_host}"):
                    return True
                continue

            parsed_rule = urlsplit(f"http://{rule}")
            rule_host = (parsed_rule.hostname or "").lower()
            rule_port = parsed_rule.port
            if not rule_host:
                continue

            if rule_port is not None:
                if target_host == rule_host and target_port == rule_port:
                    return True
            else:
                if target_host == rule_host:
                    return True

        return False


settings = Settings()
