import json
import logging
from pathlib import Path

import httpx

from guardrail.config import Settings
from guardrail.main import _configure_json_access_logging, create_app
from tests.conftest import app_client


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        gateway_db_path=str(tmp_path / "gateway.db"),
        **overrides,
    )


async def test_healthz_does_not_check_dependencies(tmp_path):
    async with app_client(create_app(_settings(tmp_path))) as client:
        r = await client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


async def test_request_body_limit_returns_413(tmp_path):
    async with app_client(
        create_app(_settings(tmp_path, max_request_bytes=64))
    ) as client:
        r = await client.post(
            "/v1/sessions",
            json={"agent_id": "ops_agent", "task_id": "x" * 200},
        )
        assert r.status_code == 413


async def test_request_body_limit_handles_invalid_content_length(tmp_path):
    async with app_client(
        create_app(_settings(tmp_path, max_request_bytes=64))
    ) as client:
        request = client.build_request(
            "POST",
            "/v1/sessions",
            content=b'{"agent_id":"ops_agent"}',
            headers={"content-length": "not-a-number"},
        )
        r = await client.send(request)
        assert r.status_code == 400


async def test_request_body_limit_blocks_chunked_body_without_content_length(tmp_path):
    async with app_client(
        create_app(_settings(tmp_path, max_request_bytes=32))
    ) as client:
        async def body():
            yield b'{"agent_id":"ops_agent","task_id":"'
            yield b"x" * 128
            yield b'"}'

        r = await client.post("/v1/sessions", content=body())
        assert r.status_code == 413


async def test_rate_limit_returns_429(tmp_path):
    async with app_client(
        create_app(
            _settings(
                tmp_path,
                rate_limit_requests=2,
                rate_limit_window_seconds=60,
            )
        )
    ) as client:
        for _ in range(2):
            r = await client.post(
                "/v1/sessions",
                json={"agent_id": "ops_agent", "task_id": "rate"},
            )
            assert r.status_code == 200
        blocked = await client.post(
            "/v1/sessions",
            json={"agent_id": "ops_agent", "task_id": "rate"},
        )
        assert blocked.status_code == 429
        assert blocked.headers["Retry-After"].isdigit()


async def test_access_log_is_json(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="guardrail.access")
    async with app_client(create_app(_settings(tmp_path))) as client:
        r = await client.get("/healthz")
        assert r.status_code == 200
        assert r.headers["X-Request-ID"]

    access = [r for r in caplog.records if r.name == "guardrail.access"]
    assert access
    payload = json.loads(access[-1].message)
    assert payload["method"] == "GET"
    assert payload["path"] == "/healthz"
    assert payload["status"] == 200
    assert payload["request_id"]
    assert "latency_ms" in payload


def test_json_access_logger_uses_bare_message_formatter():
    logger = logging.getLogger("guardrail.access.test")
    _configure_json_access_logging(logger)
    payload = {"request_id": "r-1", "status": 200}
    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        1,
        json.dumps(payload),
        (),
        None,
    )
    assert logger.handlers[-1].formatter.format(record) == json.dumps(payload)


async def test_readyz_returns_valid_json_when_shop_is_unavailable(tmp_path):
    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "down"})

    async with app_client(
        create_app(
            _settings(tmp_path),
            shop_transport=httpx.MockTransport(unavailable),
        )
    ) as client:
        response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "unavailable"
        assert response.json()["problems"]


async def test_production_profile_health_auth_and_rate_limit(tmp_path):
    def shop_ready(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/readyz"
        return httpx.Response(200, json={"status": "ok"})

    settings = _settings(
        tmp_path,
        environment="production",
        console_key="console-secret",
        console_approver_id="security_lead",
        agent_api_keys={"agent-secret": "ops_agent"},
        approver_api_keys={"approver-secret": "security_lead"},
        rate_limit_requests=1,
    )
    async with app_client(
        create_app(settings, shop_transport=httpx.MockTransport(shop_ready))
    ) as client:
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.headers["X-Content-Type-Options"] == "nosniff"
        assert health.headers["X-Frame-Options"] == "DENY"
        assert health.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in health.headers["Content-Security-Policy"]
        assert health.headers["Strict-Transport-Security"].startswith("max-age=")
        assert (await client.get("/readyz")).status_code == 200
        denied = await client.post("/v1/sessions", json={"agent_id": "ops_agent"})
        assert denied.status_code == 401

        headers = {"X-API-Key": "agent-secret"}
        allowed = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"agent_id": "ops_agent", "task_id": "prod-smoke"},
        )
        limited = await client.post(
            "/v1/sessions",
            headers=headers,
            json={"agent_id": "ops_agent", "task_id": "prod-smoke"},
        )
        assert allowed.status_code == 200
        assert limited.status_code == 429
        assert limited.headers["Retry-After"].isdigit()
