import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.config import Settings
from guardrail.main import create_app
from shop.main import create_app as create_shop_app

AGENT_HEADERS = {"X-API-Key": "ops-key"}
PRICING_HEADERS = {"X-API-Key": "pricing-key"}
APPROVER_HEADERS = {"X-API-Key": "approver-key"}


@pytest.fixture
async def auth_client(tmp_path):
    shop_app = create_shop_app(str(tmp_path / "shop.db"))
    async with LifespanManager(shop_app):
        app = create_app(
            Settings(
                shop_base_url="http://shop.test",
                gateway_db_path=str(tmp_path / "gateway.db"),
                agent_api_keys={
                    "ops-key": "ops_agent",
                    "pricing-key": "pricing_agent",
                },
                approver_api_keys={"approver-key": "boss"},
            ),
            shop_transport=httpx.ASGITransport(app=shop_app),
        )
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://auth"
            ) as client:
                yield client


async def _new_session(client, headers=AGENT_HEADERS, agent_id="ops_agent"):
    return await client.post(
        "/v1/sessions",
        headers=headers,
        json={"agent_id": agent_id, "task_id": "auth-task"},
    )


async def test_agent_endpoint_requires_api_key(auth_client):
    r = await auth_client.post(
        "/v1/sessions",
        json={"agent_id": "ops_agent", "task_id": "auth-task"},
    )
    assert r.status_code == 401


async def test_agent_key_cannot_create_session_for_another_agent(auth_client):
    r = await _new_session(auth_client, agent_id="pricing_agent")
    assert r.status_code == 403


async def test_bearer_authorization_is_supported(auth_client):
    r = await _new_session(
        auth_client,
        headers={"Authorization": "Bearer ops-key"},
    )
    assert r.status_code == 200


async def test_agent_key_cannot_use_another_agents_session(auth_client):
    session = await _new_session(auth_client)
    assert session.status_code == 200
    sid = session.json()["session_id"]

    r = await auth_client.post(
        "/v1/tools/list_products",
        headers=PRICING_HEADERS,
        json={"session_id": sid, "args": {}},
    )
    assert r.status_code == 403


async def test_approver_key_controls_plan_resolution(auth_client):
    session = await _new_session(auth_client)
    assert session.status_code == 200
    sid = session.json()["session_id"]

    prime = await auth_client.post(
        "/v1/tools/list_products",
        headers=AGENT_HEADERS,
        json={"session_id": sid, "args": {}},
    )
    assert prime.status_code == 200

    plan = await auth_client.post(
        "/v1/plans",
        headers=AGENT_HEADERS,
        json={
            "session_id": sid,
            "intent": "auth approval",
            "actions": [
                {
                    "step": i,
                    "tool": "update_price",
                    "args": {"product_id": "p-iphone", "delta_pct": -8.0},
                }
                for i in range(5)
            ],
        },
    )
    assert plan.status_code == 200
    plan_id = plan.json()["plan_id"]

    denied = await auth_client.post(
        f"/v1/plans/{plan_id}/resolve",
        json={"resolution": "approve", "decided_by": "attacker"},
    )
    assert denied.status_code == 401

    approved = await auth_client.post(
        f"/v1/plans/{plan_id}/resolve",
        headers=APPROVER_HEADERS,
        json={"resolution": "approve", "decided_by": "attacker"},
    )
    assert approved.status_code == 200
    assert approved.json()["decided_by"] == "boss"


async def test_console_requires_approver_key_when_auth_enabled(auth_client):
    denied = await auth_client.get("/console")
    assert denied.status_code == 401

    allowed = await auth_client.get("/console", headers=APPROVER_HEADERS)
    assert allowed.status_code == 200


def test_half_configured_auth_refuses_to_start(tmp_path):
    with pytest.raises(RuntimeError, match="认证配置不完整"):
        create_app(
            Settings(
                gateway_db_path=str(tmp_path / "gateway.db"),
                agent_api_keys={"ops-key": "ops_agent"},
            )
        )


def test_agent_and_approver_keys_cannot_be_reused(tmp_path):
    with pytest.raises(RuntimeError, match="不能复用"):
        create_app(
            Settings(
                gateway_db_path=str(tmp_path / "gateway.db"),
                agent_api_keys={"shared": "ops_agent"},
                approver_api_keys={"shared": "boss"},
            )
        )


def test_production_requires_console_and_agent_auth(tmp_path):
    with pytest.raises(RuntimeError, match="生产环境必须"):
        create_app(
            Settings(
                gateway_db_path=str(tmp_path / "gateway.db"),
                environment="production",
                agent_api_keys={"agent-key": "ops_agent"},
                approver_api_keys={"approver-key": "boss"},
            )
        )


def test_production_requires_console_approver_identity(tmp_path):
    with pytest.raises(RuntimeError, match="GUARDRAIL_CONSOLE_APPROVER_ID"):
        create_app(
            Settings(
                gateway_db_path=str(tmp_path / "gateway.db"),
                environment="production",
                console_key="console-secret",
                agent_api_keys={"agent-key": "ops_agent"},
                approver_api_keys={"approver-key": "boss"},
            )
        )
