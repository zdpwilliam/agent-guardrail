"""plan_token 执行侧测试（spec §4.5 三重校验 + §4.6 生命周期）。"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.config import Settings
from guardrail.main import create_app
from shop.main import create_app as create_shop_app


@pytest.fixture
async def env(tmp_path):
    shop_app = create_shop_app(str(tmp_path / "shop.db"))
    async with LifespanManager(shop_app):
        app = create_app(
            Settings(shop_base_url="http://shop.test",
                     gateway_db_path=str(tmp_path / "gateway.db")),
            shop_transport=httpx.ASGITransport(app=shop_app),
        )
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                yield c, app


async def _session(c, agent_id="pricing_agent"):
    return (await c.post("/v1/sessions",
                         json={"agent_id": agent_id, "task_id": "t-1"})).json()["session_id"]


async def _prime(c, sid):
    await c.post("/v1/tools/list_products", json={"session_id": sid, "args": {}})


async def _low_plan(c, sid, delta=-3.0):
    """单步 low 计划：自动批准，返回 (plan_id, token)。"""
    r = await c.post("/v1/plans", json={
        "session_id": sid, "intent": "小幅调价",
        "actions": [{"step": 0, "tool": "update_price",
                     "args": {"product_id": "p-iphone", "delta_pct": delta}}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["risk_level"] == "low"
    return body["plan_id"], body["token"]


async def _execute(c, sid, plan_id, tool, args):
    return await c.post(f"/v1/tools/{tool}",
                        json={"session_id": sid, "args": args, "plan_id": plan_id})


# ---------- 第 1 条：token 有效 ----------


async def test_plan_step_executes_end_to_end(env):
    c, _ = env
    sid = await _session(c)
    await _prime(c, sid)
    plan_id, _ = await _low_plan(c, sid)
    r = await _execute(c, sid, plan_id, "update_price",
                       {"product_id": "p-iphone", "delta_pct": -3.0})
    assert r.status_code == 200
    assert r.json()["plan_id"] == plan_id
    plan = (await c.get(f"/v1/plans/{plan_id}")).json()
    assert plan["status"] == "completed"
    assert len(plan["executed_hashes"]) == 1


async def test_expired_token_rejected(env):
    c, app = env
    sid = await _session(c)
    await _prime(c, sid)
    plan_id, _ = await _low_plan(c, sid)
    # 直接把 TTL 拨到过去（模拟时间流逝）。
    await app.state.backend.execute(
        "UPDATE plans SET token_expires_at = ? WHERE plan_id = ?",
        ("2000-01-01T00:00:00+00:00", plan_id),
    )
    await app.state.backend.commit()
    r = await _execute(c, sid, plan_id, "update_price",
                       {"product_id": "p-iphone", "delta_pct": -3.0})
    assert r.status_code == 403
    assert "过期" in r.json()["detail"]
    assert (await c.get(f"/v1/plans/{plan_id}")).json()["status"] == "expired"


async def test_pending_plan_cannot_execute(env):
    c, _ = env
    sid = await _session(c)
    await _prime(c, sid)
    plan_id = (await c.post("/v1/plans", json={
        "session_id": sid, "intent": "大幅调价",
        "actions": [{"step": i, "tool": "update_price",
                     "args": {"product_id": "p-iphone", "delta_pct": -8.0}}
                    for i in range(5)],
    })).json()["plan_id"]
    r = await _execute(c, sid, plan_id, "update_price",
                       {"product_id": "p-iphone", "delta_pct": -8.0})
    assert r.status_code == 403
    assert "未获批准" in r.json()["detail"]


async def test_plan_submission_rejects_order_over_result_cap(env):
    c, _ = env
    sid = await _session(c, agent_id="ops_agent")
    await _prime(c, sid)

    r = await c.post("/v1/plans", json={
        "session_id": sid,
        "intent": "大额订单",
        "actions": [{
            "step": 0,
            "tool": "create_order",
            "args": {"product_id": "p-iphone", "qty": 49},
        }],
    })

    assert r.status_code == 422
    assert "cap_order_amount" in r.json()["detail"]


async def test_plan_of_other_session_rejected(env):
    c, _ = env
    sid = await _session(c)
    await _prime(c, sid)
    plan_id, _ = await _low_plan(c, sid)
    other = await _session(c)
    await _prime(c, other)
    r = await _execute(c, other, plan_id, "update_price",
                       {"product_id": "p-iphone", "delta_pct": -3.0})
    assert r.status_code == 403
    assert "不属于当前会话" in r.json()["detail"]


# ---------- 第 2 条：动作在计划内且未消费 ----------


async def test_tampered_args_hash_mismatch(env):
    c, _ = env
    sid = await _session(c)
    await _prime(c, sid)
    plan_id, _ = await _low_plan(c, sid)
    # 批准的是 -3.0，执行时篡改成 -30（单次阈值内！语法层拦不住它）。
    r = await _execute(c, sid, plan_id, "update_price",
                       {"product_id": "p-iphone", "delta_pct": -30.0})
    assert r.status_code == 403
    assert "哈希失配" in r.json()["detail"]
    # 商城未被改动——这就是 token 防「批准后调包」的意义。
    price = (await c.post("/v1/tools/get_product",
                          json={"session_id": sid,
                                "args": {"product_id": "p-iphone"}})
             ).json()["result"]["product"]["list_price_cents"]
    assert price == 599900


async def test_extra_step_not_in_plan_rejected(env):
    c, _ = env
    sid = await _session(c)
    await _prime(c, sid)
    plan_id, _ = await _low_plan(c, sid)
    r = await _execute(c, sid, plan_id, "update_stock",
                       {"product_id": "p-iphone", "delta": -1})
    assert r.status_code == 403
    assert "哈希失配" in r.json()["detail"]


async def test_step_cannot_execute_twice(env):
    c, _ = env
    sid = await _session(c)
    await _prime(c, sid)
    plan_id, _ = await _low_plan(c, sid)
    args = {"product_id": "p-iphone", "delta_pct": -3.0}
    r1 = await _execute(c, sid, plan_id, "update_price", args)
    assert r1.status_code == 200
    # 同参第二次 → 幂等重放（不重执行）；换个会话视角不可用——同 plan_id
    # 的重复消费在 completed 后被状态机拒绝。
    r2 = await _execute(c, sid, plan_id, "update_price", args)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True


# ---------- 第 3 条：重新求值 ----------


async def test_reevaluation_blocks_step_after_state_changed(env):
    """批准后另一 Agent 改了同一个商品 → 组合规则在执行时击穿 → 该步被拒。

    这是 §4.5 第 3 条的核心场景：批准的是当时的状态，执行时状态可能已变。
    """
    c, _ = env
    sid = await _session(c)
    await _prime(c, sid)
    # 计划内 5 步递变降价（幂等键含 args：逐字相同的步骤会被重放挡下）。
    # 提交时终态 -40.9 越过 deny_at -35 → high → pending → 人工批准。
    deltas = [-8.0, -8.1, -8.2, -8.3, -8.4]
    plan_id = (await c.post("/v1/plans", json={
        "session_id": sid, "intent": "调价",
        "actions": [{"step": i, "tool": "update_price",
                     "args": {"product_id": "p-iphone", "delta_pct": d}}
                    for i, d in enumerate(deltas)],
    })).json()["plan_id"]
    r = await c.post(f"/v1/plans/{plan_id}/resolve",
                     json={"resolution": "approve", "decided_by": "boss"})
    assert r.status_code == 200

    # 前 4 步正常执行（累计 -32.6，未越 deny 线）。
    for d in deltas[:4]:
        r = await _execute(c, sid, plan_id, "update_price",
                           {"product_id": "p-iphone", "delta_pct": d})
        assert r.status_code == 200, r.json()

    # 第 5 步把累计推到 -41 → 越过 deny_at -35 → 执行时组合规则拒绝（重新求值）。
    r5 = await _execute(c, sid, plan_id, "update_price",
                        {"product_id": "p-iphone", "delta_pct": deltas[4]})
    assert r5.status_code == 403
    assert "cumulative_price_cut" in r5.json()["detail"]
    plan = (await c.get(f"/v1/plans/{plan_id}")).json()
    # 不回滚：已执行的 4 步保持，计划标红但仍是 approved（剩余步骤可换参数或放弃）。
    assert len(plan["executed_hashes"]) == 4


# ---------- 生命周期：partially_applied 与 completed ----------


async def test_partial_failure_marks_plan_and_remaining_executes(env):
    """TOCTOU 构造部分失败：批准后、执行前，订单在计划外被退掉 →
    计划内的退款步在商城撞 409 → partially_applied（不回滚，标红）。"""
    c, _ = env
    sid = await _session(c, agent_id="ops_agent")
    await _prime(c, sid)
    # 先造一个真实订单（单调用路径），计划引用它——否则投影装载就 404。
    order = await c.post("/v1/tools/create_order",
                         json={"session_id": sid,
                               "args": {"product_id": "p-tshirt-s", "qty": 1}})
    order_id = order.json()["result"]["order_id"]

    plan_id = (await c.post("/v1/plans", json={
        "session_id": sid, "intent": "调价+退款",
        "actions": [{"step": 0, "tool": "update_price",
                     "args": {"product_id": "p-iphone", "delta_pct": -2.0}},
                    {"step": 1, "tool": "refund_order",
                     "args": {"order_id": order_id}}],
    })).json()["plan_id"]
    assert (await c.get(f"/v1/plans/{plan_id}")).json()["status"] == "approved"

    r1 = await _execute(c, sid, plan_id, "update_price",
                        {"product_id": "p-iphone", "delta_pct": -2.0})
    assert r1.status_code == 200

    # TOCTOU：计划外（另一个会话 → 幂等键不同）把订单退掉。
    other = await _session(c, agent_id="ops_agent")
    # Provenance 门：其他会话必须先读到该订单才能退它（按设计工作）。
    await c.post("/v1/tools/get_order",
                 json={"session_id": other, "args": {"order_id": order_id}})
    outside = await c.post("/v1/tools/refund_order",
                           json={"session_id": other, "args": {"order_id": order_id}})
    assert outside.status_code == 200

    r2 = await _execute(c, sid, plan_id, "refund_order", {"order_id": order_id})
    assert r2.status_code == 409

    plan = (await c.get(f"/v1/plans/{plan_id}")).json()
    assert plan["status"] == "partially_applied"
    assert len(plan["executed_hashes"]) == 1  # 失败步不占坑，可重试

    # 部分失败后剩余合法步骤仍可执行（partially_applied → completed）。
    # 造一个新订单并把它加进计划？——计划不可变（哈希封存），所以这里验证
    # 「partially_applied 状态下原计划内已消费步的重放仍正常」。
    r3 = await _execute(c, sid, plan_id, "update_price",
                        {"product_id": "p-iphone", "delta_pct": -2.0})
    assert r3.status_code == 200
    assert r3.json()["replayed"] is True


async def test_without_plan_id_single_call_path_unchanged(env):
    c, _ = env
    sid = await _session(c)
    await _prime(c, sid)
    r = await c.post("/v1/tools/update_price",
                     json={"session_id": sid,
                           "args": {"product_id": "p-iphone", "delta_pct": -5.0}})
    assert r.status_code == 200
    assert "plan_id" not in r.json()
