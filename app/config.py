"""Application configuration.

Settings are read from environment variables (or a local `.env` file) so the
same image can run in dev, CI and production without code changes.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application metadata --------------------------------------------
    APP_NAME: str = "Geospatial File Measurement API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # --- Persistence ------------------------------------------------------
    DATABASE_URL: str = "sqlite:///./geospatial.db"
    STORAGE_DIR: Path = Path("./storage")

    # --- Upload safety limits --------------------------------------------
    # Guard rails for untrusted uploads. A zip archive is attacker-controlled
    # input: without these, a 1 MB file can expand to fill the disk.
    MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024          # 100 MB
    MAX_UNCOMPRESSED_BYTES: int = 500 * 1024 * 1024    # 500 MB
    MAX_COMPRESSION_RATIO: int = 200

    # --- Measurement tuning ----------------------------------------------
    # A UTM zone is 6 degrees of longitude wide. Beyond roughly that span the
    # transverse-Mercator distortion makes planar measurement unacceptable, so
    # we fall back to geodesic computation on the ellipsoid.
    MAX_UTM_LON_SPAN_DEGREES: float = 6.0
    # UTM is only defined between 80S and 84N.
    UTM_MAX_ABS_LATITUDE: float = 84.0

    # --- API behaviour ----------------------------------------------------
    DEFAULT_PAGE_SIZE: int = 100
    MAX_PAGE_SIZE: int = 1000
    CORS_ORIGINS: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        if self.CORS_ORIGINS.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    """Cached so the `.env` file is parsed exactly once per process."""
    settings = Settings()
    settings.STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
