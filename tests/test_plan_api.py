"""计划 API 端到端测试（spec §4.2/§4.4）。"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.config import Settings
from guardrail.main import create_app
from shop.main import create_app as create_shop_app


@pytest.fixture
async def client(tmp_path):
    shop_app = create_shop_app(str(tmp_path / "shop.db"))
    async with LifespanManager(shop_app):
        settings = Settings(
            shop_base_url="http://shop.test",
            gateway_db_path=str(tmp_path / "gateway.db"),
        )
        app = create_app(settings, shop_transport=httpx.ASGITransport(app=shop_app))
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                yield c, app


async def _session(client, agent_id="ops_agent"):
    r = await client.post("/v1/sessions", json={"agent_id": agent_id, "task_id": "t-1"})
    return r.json()["session_id"]


async def _prime(client, sid):
    r = await client.post("/v1/tools/list_products", json={"session_id": sid, "args": {}})
    assert r.status_code == 200


def _plan_body(sid, actions, intent="清仓"):
    return {"session_id": sid, "intent": intent,
            "actions": [{"step": i, "tool": t, "args": a}
                        for i, (t, a) in enumerate(actions)]}


async def test_low_plan_auto_approves_with_token(client):
    c, _ = client
    sid = await _session(c)
    await _prime(c, sid)
    r = await c.post("/v1/plans", json=_plan_body(sid, [
        ("update_price", {"product_id": "p-iphone", "delta_pct": -3.0}),
    ]))
    assert r.status_code == 200
    body = r.json()
    assert body["risk_level"] == "low"
    assert body["token"] is not None
    assert body["token"]["plan_id"] == body["plan_id"]
    assert len(body["token"]["action_hashes"]) == 1


async def test_high_plan_goes_pending(client):
    c, _ = client
    sid = await _session(c)
    await _prime(c, sid)
    r = await c.post("/v1/plans", json=_plan_body(sid, [
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
    ]))
    assert r.status_code == 200
    body = r.json()
    assert body["risk_level"] == "high"
    assert body["token"] is None
    assert body["plan_id"]


async def test_rejected_plan_returns_422(client):
    c, _ = client
    sid = await _session(c)
    await _prime(c, sid)
    r = await c.post("/v1/plans", json=_plan_body(sid, [
        ("update_price", {"product_id": "p-iphone", "delta_pct": -50.0}),
    ]))
    assert r.status_code == 422
    assert "max_single_price_cut" in r.json()["detail"]


async def test_resolve_approve_issues_token(client):
    c, app = client
    sid = await _session(c)
    await _prime(c, sid)
    plan_id = (await c.post("/v1/plans", json=_plan_body(sid, [
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
    ]))).json()["plan_id"]

    r = await c.post(f"/v1/plans/{plan_id}/resolve",
                     json={"resolution": "approve", "decided_by": "boss"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"
    assert body["token"]["plan_id"] == plan_id

    plan = (await c.get(f"/v1/plans/{plan_id}")).json()
    assert plan["status"] == "approved"
    assert plan["decided_by"] == "boss"


async def test_resolve_reject_and_double_resolve(client):
    c, _ = client
    sid = await _session(c)
    await _prime(c, sid)
    plan_id = (await c.post("/v1/plans", json=_plan_body(sid, [
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
    ]))).json()["plan_id"]

    r1 = await c.post(f"/v1/plans/{plan_id}/resolve",
                      json={"resolution": "reject", "decided_by": "boss"})
    assert r1.status_code == 200
    r2 = await c.post(f"/v1/plans/{plan_id}/resolve",
                      json={"resolution": "approve", "decided_by": "other"})
    assert r2.status_code == 409


async def test_pending_list_shows_high_plan(client):
    c, _ = client
    sid = await _session(c)
    await _prime(c, sid)
    plan_id = (await c.post("/v1/plans", json=_plan_body(sid, [
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
        ("update_price", {"product_id": "p-iphone", "delta_pct": -8.0}),
    ]))).json()["plan_id"]
    ids = [p["plan_id"] for p in (await c.get("/v1/plans?status=pending")).json()]
    assert plan_id in ids


async def test_unknown_session_404(client):
    c, _ = client
    r = await c.post("/v1/plans", json=_plan_body("s-nope", [
        ("update_price", {"product_id": "p-iphone", "delta_pct": -3.0}),
    ]))
    assert r.status_code == 404
