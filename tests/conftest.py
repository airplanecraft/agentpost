import os

import pytest

from agentposthub import flow
from agentposthub.config import reset_settings_cache


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path, monkeypatch):
    """Point every module-level cache at a throwaway sqlite file so tests
    never touch the developer's real ledger or trust memory."""
    monkeypatch.setenv("APH_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("APH_STORE_PATH", str(tmp_path / "test_store.db"))
    monkeypatch.setenv("PUBLISH_MODE", "dry_run")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("IMAGE_GATEWAY_URL", raising=False)
    reset_settings_cache()
    flow.get_db.cache_clear()
    flow.get_store.cache_clear()
    flow.get_graph.cache_clear()
    yield
    flow.get_db.cache_clear()
    flow.get_store.cache_clear()
    flow.get_graph.cache_clear()
