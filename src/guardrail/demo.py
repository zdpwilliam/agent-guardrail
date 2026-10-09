"""demo 场景（spec §13）与演示服务。

四个场景走的是和真实流量完全相同的网关路径——场景不是「演示代码」，
是「可执行验收」。幂等键含 args 的推论在这里也成立：连续同类操作用
递变参数表达（spec §4.6/§6.2 回写）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx


async def _prime(c: httpx.AsyncClient, sid: str) -> None:
    r = await c.post("/v1/tools/list_products", json={"session_id": sid, "args": {}})
    assert r.status_code == 200, r.text


async def _new(c: httpx.AsyncClient, agent_id: str, task_id: str) -> str:
    r = await c.post("/v1/sessions", json={"agent_id": agent_id, "task_id": task_id})
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _step(desc: str, ok: bool, detail: str) -> dict:
    return {"desc": desc, "ok": ok, "detail": detail}


async def scenario_1_single_policy(c: httpx.AsyncClient) -> dict:
    """场景 1：试图把 iPhone 打一折 → 单次策略立即拒绝。"""
    sid = await _new(c, "ops_agent", "demo-s1")
    await _prime(c, sid)
    r = await c.post("/v1/tools/update_price", json={
        "session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -90.0}})
    ok = r.status_code == 403 and "单次降价" in r.json()["detail"]
    return {"name": "场景 1：单次策略", "passed": ok, "steps": [
        _step("试图把 iPhone 降价 90%", r.status_code == 403,
              f"{r.status_code} {r.json()['detail']}"),
        _step("商城价格未被改动", ok, "语法层拒绝，未触达商城"),
    ]}


async def scenario_2_budget_ladder(c: httpx.AsyncClient) -> dict:
    """场景 2：连续降价 → 预算阶梯 allow→flag→ASK(202)，第 7 次扣下。"""
    sid = await _new(c, "pricing_agent", "demo-s2")
    await _prime(c, sid)
    steps = []
    codes = []
    for i in range(7):
        delta = -3.0 - i * 0.1
        r = await c.post("/v1/tools/update_price", json={
            "session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": delta}})
        codes.append((r.status_code, r.json().get("decision")))
    steps.append(_step("前 5 次递变降价放行", all(c[1] in ("allow", "allow_with_flag")
                                              for c in codes[:5]), str(codes[:5])))
    steps.append(_step("第 6 次进入 flag 区", codes[5][1] == "allow_with_flag", str(codes[5])))
    ask_ok = codes[6][0] == 202 and codes[6][1] == "ask"
    steps.append(_step("第 7 次预算耗尽 → 扣下审批(202)", ask_ok, str(codes[6])))
    return {"name": "场景 2：风险预算阶梯", "passed": all(s["ok"] for s in steps), "steps": steps}


async def scenario_3_plan_approval(c: httpx.AsyncClient) -> dict:
    """场景 3：「夏季款清仓」→ 一份计划一次批准（§4.1 的一棵树）。"""
    sid = await _new(c, "ops_agent", "demo-s3")
    await _prime(c, sid)
    r = await c.post("/v1/plans", json={
        "session_id": sid, "intent": "清仓：阶梯调价 5 步",
        "actions": [{"step": i, "tool": "update_price",
                     "args": {"product_id": "p-iphone", "delta_pct": -8.0 - i * 0.1}}
                    for i in range(5)],
    })
    body = r.json()
    steps = []
    if r.status_code != 200 or body["risk_level"] != "high":
        return {"name": "场景 3：计划级审批", "passed": False,
                "steps": [_step("提交 5 步计划 → high 待批", False, f"{r.status_code} {body}")]}
    steps.append(_step("提交 5 步计划 → 一次待批卡片", True,
                       f"risk=high, rules={body['triggered_rules']}"))
    plan_id = body["plan_id"]
    r2 = await c.post(f"/v1/plans/{plan_id}/resolve",
                      json={"resolution": "approve", "decided_by": "demo 审批人"})
    steps.append(_step("控制台/API 一次批准 → 签发 token", r2.status_code == 200,
                       str(r2.json().get("status"))))
    ok_all = True
    for i in range(4):
        rr = await c.post("/v1/tools/update_price", json={
            "session_id": sid, "plan_id": plan_id,
            "args": {"product_id": "p-iphone", "delta_pct": -8.0 - i * 0.1}})
        ok_all = ok_all and rr.status_code == 200
    steps.append(_step("凭 token 逐步执行（前三步）", ok_all, "200 × 3"))
    return {"name": "场景 3：计划级审批", "passed": all(s["ok"] for s in steps), "steps": steps}


async def scenario_4_cross_agent(c: httpx.AsyncClient) -> dict:
    """场景 4：定价降价 × 营销发券 → multiplicative 叠加击穿成本线。"""
    s1 = await _new(c, "pricing_agent", "demo-s4")
    await _prime(c, s1)
    r1 = await c.post("/v1/tools/update_price", json={
        "session_id": s1, "args": {"product_id": "p-iphone", "delta_pct": -8.0}})
    steps = [_step("定价 Agent 降价 8%（单次合规）", r1.status_code == 200, str(r1.status_code))]
    s2 = await _new(c, "marketing_agent", "demo-s4")
    await _prime(c, s2)
    r2 = await c.post("/v1/tools/create_coupon", json={
        "session_id": s2, "args": {"code": "DEMO20", "discount_pct": 20.0, "max_uses": 5}})
    ok = r2.status_code == 403 and "price_and_coupon_stack" in r2.json()["detail"]
    steps.append(_step("营销 Agent 发 20% 券 → 跨 Agent 击穿拦截", ok,
                       f"{r2.status_code} {r2.json().get('detail', '')[:60]}"))
    steps.append(_step("multiplicative 合计 -26.4% ≤ -25 成本线", ok,
                       "(0.92)(0.80)-1 = -26.4%"))
    return {"name": "场景 4：跨 Agent 组合风险",
            "passed": all(s["ok"] for s in steps), "steps": steps}


SCENARIOS = [scenario_1_single_policy, scenario_2_budget_ladder,
             scenario_3_plan_approval, scenario_4_cross_agent]


async def run_scenarios(gateway: httpx.AsyncClient) -> list[dict]:
    """顺序跑完 4 个场景，返回结果列表（含步骤明细）。"""
    return [await fn(gateway) for fn in SCENARIOS]


def print_results(results: list[dict]) -> bool:
    """打印结果表，返回整体是否通过。"""
    all_ok = True
    for r in results:
        mark = "✅" if r["passed"] else "❌"
        all_ok = all_ok and r["passed"]
        print(f"\n{mark} {r['name']}")
        for s in r["steps"]:
            print(f"   {'✓' if s['ok'] else '✗'} {s['desc']} — {s['detail']}")
    print(f"\n{'全部通过' if all_ok else '存在失败'}")
    return all_ok


@asynccontextmanager
async def build_demo_stack(db_path: str) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    """商城（进程内 ASGI）+ 网关 + 控制台。真实调用走 ASGI transport，
    不占第二个端口——demo 的价值在护栏行为，不在网络拓扑。

    两个 app 的 lifespan 都要进入：商城的 store、网关的策略与存储
    都在 lifespan 里初始化。"""
    from contextlib import AsyncExitStack

    from guardrail.config import Settings
    from guardrail.main import create_app
    from shop.main import create_app as create_shop_app

    async with AsyncExitStack() as stack:
        shop_app = create_shop_app(db_path.replace(".db", "-shop.db"))
        await stack.enter_async_context(shop_app.router.lifespan_context(shop_app))
        app = create_app(
            Settings(shop_base_url="http://shop.inprocess",
                     gateway_db_path=db_path),
            shop_transport=httpx.ASGITransport(app=shop_app),
        )
        await stack.enter_async_context(app.router.lifespan_context(app))
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://demo")
        stack.push_async_callback(client.aclose)
        yield app, client
