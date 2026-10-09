"""P3:HTTP 层并发安全——生产可用性的正确性部分。

乐观锁 / CAS / 预算扣减在并发请求下的行为(store 层并发见 test_concurrency.py):
- 并发预算扣减:总扣减 ≤ 预算,不超扣
- 并发计划消费:同一动作哈希只被消费一次(CAS)
- 并发同实体写:全部有结论,无 500
"""

import asyncio

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
        app = create_app(
            Settings(shop_base_url="http://shop.test",
                     gateway_db_path=str(tmp_path / "gateway.db")),
            shop_transport=httpx.ASGITransport(app=shop_app),
        )
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://t") as c:
                yield c


async def _new_session(c: httpx.AsyncClient, task: str) -> str:
    r = await c.post("/v1/sessions", json={"agent_id": "ops_agent",
                                           "task_id": task})
    assert r.status_code == 200, r.text
    sid = r.json()["session_id"]
    # provenance 门 prime：写操作要求实体先被本会话读过（与评测 runner 一致）。
    await c.post("/v1/tools/list_products",
                 json={"session_id": sid, "args": {}})
    return sid


async def test_concurrent_budget_never_overspends(client):
    """20 个并发敏感写抢同一预算:成功数 × 单次扣减 ≤ 初始预算。"""
    sid = await _new_session(client, "conc-budget")
    results = await asyncio.gather(*[
        client.post("/v1/tools/update_price", json={
            "session_id": sid,
            "args": {"product_id": f"p-{i}", "delta_pct": -3.0}})
        for i in range(20)
    ])
    codes = [r.status_code for r in results]
    allowed = sum(1 for code in codes if code == 200)
    blocked = sum(1 for code in codes if code in (403, 202, 429))
    # 全部请求有明确结论(不 500)——fail-closed 不等于崩溃
    assert all(code < 500 for code in codes), codes
    # 初始预算 1.0,sensitive_write 0.15 → 最多成功 6 笔(0.9)
    assert allowed <= 6, f"预算超扣: {allowed} 笔成功"
    assert allowed + blocked == 20


async def test_concurrent_same_entity_writes_all_resolved(client):
    """同一实体并发调价 ×10:全部有结论、无 500(乐观锁重试后收敛)。"""
    sid = await _new_session(client, "conc-write")
    results = await asyncio.gather(*[
        client.post("/v1/tools/update_price", json={
            "session_id": sid,
            "args": {"product_id": "p-iphone", "delta_pct": -1.0}})
        for _ in range(10)
    ])
    codes = [r.status_code for r in results]
    assert all(code < 500 for code in codes), codes


async def test_concurrent_plan_step_consumed_once(client):
    """同一计划步并发执行 ×5:CAS 保证只消费一次,其余 409。"""
    sid = await _new_session(client, "conc-plan")
    plan = await client.post("/v1/plans", json={
        "session_id": sid, "intent": "并发消费测试",
        "actions": [{"step": 0, "tool": "update_price",
                     "args": {"product_id": "p-iphone", "delta_pct": -3.0}}]})
    assert plan.status_code == 200, plan.text
    body = plan.json()
    if body["risk_level"] in ("medium", "high"):
        pytest.skip("计划被扣审,与并发消费语义无关")
    plan_id = body["plan_id"]
    results = await asyncio.gather(*[
        client.post("/v1/tools/update_price", json={
            "session_id": sid, "plan_id": plan_id,
            "args": {"product_id": "p-iphone", "delta_pct": -3.0}})
        for _ in range(5)
    ])
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 1, codes
    assert all(code in (200, 409) for code in codes), codes
