import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv


def ids(value: str) -> frozenset[int]:
    return frozenset(int(part.strip()) for part in value.split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    admin_ids: frozenset[int]
    mongo_uri: str = field(default="mongodb://localhost:27017", repr=False)
    mongo_database: str = "waifu_cards"
    channel_ids: frozenset[int] = frozenset()
    default_catalog: str = "default"
    collector_enabled: bool = True
    max_download_mb: int = 15
    max_image_pixels: int = 24_000_000
    hash_distance: int = 8
    hash_margin: int = 3
    lookup_concurrency: int = 4
    lookup_cooldown_seconds: int = 2
    cache_ttl_seconds: int = 300
    cache_max_entries: int = 5000
    job_max_attempts: int = 3
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        result = cls(
            token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            admin_ids=ids(os.getenv("ADMIN_IDS", "")),
            mongo_uri=os.getenv("MONGO_URI", "mongodb://localhost:27017"),
            mongo_database=os.getenv("MONGO_DATABASE", "waifu_cards"),
            channel_ids=ids(os.getenv("ALLOWED_CHANNEL_IDS", "")),
            default_catalog=os.getenv("DEFAULT_CATALOG", "default"),
            collector_enabled=os.getenv("COLLECTOR_ENABLED", "true").lower() == "true",
            **{
                name: int(os.getenv(name.upper(), str(default)))
                for name, default in {
                    "max_download_mb": 15,
                    "max_image_pixels": 24_000_000,
                    "hash_distance": 8,
                    "hash_margin": 3,
                    "lookup_concurrency": 4,
                    "lookup_cooldown_seconds": 2,
                    "cache_ttl_seconds": 300,
                    "cache_max_entries": 5000,
                    "job_max_attempts": 3,
                }.items()
            },
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
        if not result.token or not result.admin_ids:
            raise ValueError("Set TELEGRAM_BOT_TOKEN and at least one numeric ADMIN_IDS value.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result.mongo_database):
            raise ValueError("MONGO_DATABASE must be 1–64 letters, digits, underscores or hyphens.")
        for name in (
            "max_download_mb",
            "max_image_pixels",
            "lookup_concurrency",
            "cache_ttl_seconds",
            "cache_max_entries",
            "job_max_attempts",
        ):
            if getattr(result, name) <= 0:
                raise ValueError(f"{name.upper()} must be positive.")
        if not 0 <= result.hash_distance <= 64 or not 1 <= result.hash_margin <= 64:
            raise ValueError("Hash distance must be 0–64; hash margin must be 1–64.")
        if result.lookup_cooldown_seconds < 0:
            raise ValueError("LOOKUP_COOLDOWN_SECONDS cannot be negative.")
        return result
