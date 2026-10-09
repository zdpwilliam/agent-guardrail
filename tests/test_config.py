from pathlib import Path

import pytest

from guardrail.config import Settings, get_settings


def test_settings_defaults():
    s = Settings()
    assert s.shop_base_url == "http://127.0.0.1:8100"


def test_get_settings_reads_env(monkeypatch):
    monkeypatch.setenv("SHOP_BASE_URL", "http://example.test:9999")
    get_settings.cache_clear()
    assert get_settings().shop_base_url == "http://example.test:9999"
    get_settings.cache_clear()


def test_default_policy_path_points_into_repo_policies():
    s = Settings()
    assert Path(s.policy_path).name == "single_call.yaml"
    assert Path(s.policy_path).parent.name == "policies"
    assert Path(s.policy_path).is_absolute()


def test_settings_reads_policy_path_and_ttl(monkeypatch):
    monkeypatch.setenv("GUARDRAIL_POLICY_PATH", "/tmp/p.yaml")
    monkeypatch.setenv("SESSION_TTL_MINUTES", "5")
    get_settings.cache_clear()
    s = get_settings()
    assert s.policy_path == "/tmp/p.yaml"
    assert s.session_ttl_minutes == 5
    get_settings.cache_clear()


def test_get_settings_reads_api_key_maps(monkeypatch):
    monkeypatch.setenv(
        "GUARDRAIL_AGENT_API_KEYS",
        '{"agent-key":"ops_agent"}',
    )
    monkeypatch.setenv(
        "GUARDRAIL_APPROVER_API_KEYS",
        '{"approver-key":"boss"}',
    )
    get_settings.cache_clear()
    s = get_settings()
    assert s.agent_api_keys == {"agent-key": "ops_agent"}
    assert s.approver_api_keys == {"approver-key": "boss"}
    get_settings.cache_clear()


def test_get_settings_rejects_invalid_api_key_json(monkeypatch):
    monkeypatch.setenv("GUARDRAIL_AGENT_API_KEYS", "not-json")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        get_settings()
    get_settings.cache_clear()


def test_get_settings_reads_production_security_env(monkeypatch):
    monkeypatch.setenv("GUARDRAIL_ENV", "production")
    monkeypatch.setenv("GUARDRAIL_CONSOLE_KEY", "console-secret")
    monkeypatch.setenv("GUARDRAIL_DB_BUSY_TIMEOUT_MS", "9000")
    monkeypatch.setenv("GUARDRAIL_RATE_LIMIT_REQUESTS", "50")
    monkeypatch.setenv("GUARDRAIL_RATE_LIMIT_WINDOW_SECONDS", "30")
    monkeypatch.setenv("GUARDRAIL_MAX_REQUEST_BYTES", "2048")
    get_settings.cache_clear()
    s = get_settings()
    assert s.environment == "production"
    assert s.console_key == "console-secret"
    assert s.db_busy_timeout_ms == 9000
    assert s.rate_limit_requests == 50
    assert s.rate_limit_window_seconds == 30
    assert s.max_request_bytes == 2048
    get_settings.cache_clear()


def test_get_settings_reads_retention_days(monkeypatch):
    monkeypatch.setenv("GUARDRAIL_RETENTION_DAYS", "90")
    get_settings.cache_clear()
    assert get_settings().retention_days == 90
    get_settings.cache_clear()
