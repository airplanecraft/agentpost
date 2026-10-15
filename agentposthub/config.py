"""Configuration loaded from a .env file next to the project root.

All secrets live in .env (gitignored). Copy .env.example to .env and
fill in real values; nothing here ever hardcodes a key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    # secrets / endpoints
    deepseek_api_key: str | None = None
    composio_api_key: str | None = None
    composio_mcp_url: str = "https://connect.composio.dev/mcp"

    # behaviour switches
    publish_mode: str = "dry_run"           # dry_run | live
    telegram_chat_id: str | None = None     # required for live telegram publish
    facebook_page_id: str | None = None     # your own page; absent -> skip

    # optional own image gateway; absent -> stub fallback with notice
    image_gateway_url: str | None = None

    # trip-planner source: the site's own /api/ai/chat agent
    reference_site: str = "https://1000usdinchina.com"

    # langfuse tracing (self-hosted by default); unset -> tracing off
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "http://localhost:3000"

    # storage
    db_path: Path = PROJECT_ROOT / "aph.db"
    store_path: Path = PROJECT_ROOT / "aph_store.db"

    warnings: list[str] = field(default_factory=list)


def load_settings() -> Settings:
    """Read .env (project root first, then real environment) into Settings.

    Missing optional values degrade loudly at use time, not silently here:
    each consumer checks what it needs and raises a actionable error.
    """
    load_dotenv(PROJECT_ROOT / ".env")

    s = Settings(
        deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY"),
        composio_api_key=os.environ.get("COMPOSIO_API_KEY"),
        composio_mcp_url=s_default_mcp(),
        publish_mode=(os.environ.get("PUBLISH_MODE") or "dry_run").strip().lower(),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
        facebook_page_id=os.environ.get("FACEBOOK_PAGE_ID"),
        image_gateway_url=os.environ.get("IMAGE_GATEWAY_URL"),
        reference_site=os.environ.get("REFERENCE_SITE_URL",
                                      "https://1000usdinchina.com"),
        langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
        langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
        langfuse_host=os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
        db_path=Path(os.environ.get("APH_DB_PATH") or PROJECT_ROOT / "aph.db"),
        store_path=Path(os.environ.get("APH_STORE_PATH") or PROJECT_ROOT / "aph_store.db"),
    )
    if s.publish_mode not in ("dry_run", "live"):
        raise ValueError(f"PUBLISH_MODE must be dry_run or live, got {s.publish_mode!r}")
    if s.publish_mode == "live":
        if not s.composio_api_key:
            raise ValueError("PUBLISH_MODE=live requires COMPOSIO_API_KEY in .env")
        if not s.telegram_chat_id:
            raise ValueError("PUBLISH_MODE=live requires TELEGRAM_CHAT_ID in .env")
    if not s.deepseek_api_key:
        s.warnings.append("DEEPSEEK_API_KEY missing - generation will fail; add it to .env")
    return s


def s_default_mcp() -> str:
    return os.environ.get("COMPOSIO_MCP_URL", "https://connect.composio.dev/mcp")


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cached settings; tests call load_settings() directly with env overrides."""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings_cache() -> None:
    global _settings
    _settings = None


def ssl_ctx():
    """macOS Python lacks system CAs; use certifi's bundle."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()
