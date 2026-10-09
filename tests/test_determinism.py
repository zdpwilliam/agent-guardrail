"""B3（spec §21）：求值可复现性——同一动作序列在两个独立栈上必须产生
完全相同的决策序列。

OpenAPPA 的核心承诺「same log always gets the same decision」；本项目投影
与求值本就确定性，此测试把该性质显式固化：决策不得依赖墙钟、哈希顺序、
字典序或任何进程内偶然状态。

注意：审计条目哈希**不**参与对比（含时间戳，两栈必然不同）；对比的是
每个动作的 (status, decision, reasons)。
"""

import httpx
from asgi_lifespan import LifespanManager
from hypothesis import given, settings
from hypothesis import strategies as st

from guardrail.config import Settings
from guardrail.main import create_app
from shop.main import create_app as create_shop_app

_PRODUCT_IDS = ["p-iphone", "p-tshirt-s", "p-ipad"]

# 动作池：约束在「单次阈值内、预算友好」的参数邻域，让序列长度有变化
# 且不触发幂等重放（同参即重放，对确定性无信息量）。
_ACTIONS = st.one_of(
    st.tuples(st.just("update_price"), st.sampled_from(_PRODUCT_IDS),
              st.floats(min_value=-2.95, max_value=-2.9)),
    st.tuples(st.just("update_price"), st.sampled_from(_PRODUCT_IDS),
              st.floats(min_value=-9.9, max_value=-3.1)),
    st.tuples(st.just("update_stock"), st.sampled_from(_PRODUCT_IDS),
              st.integers(min_value=-2, max_value=5)),
    st.tuples(st.just("create_coupon"), st.sampled_from(["DA", "DB", "DC", "DD"]),
              st.floats(min_value=5.0, max_value=30.0)),
    st.tuples(st.just("list_products"), st.just(""), st.just(0)),
    st.tuples(st.just("get_product"), st.sampled_from(_PRODUCT_IDS), st.just(0)),
)

# 每个动作渲染成 (tool, args)；变化后缀保证不逐字重复（幂等重放会掩盖决策路径）。
_counter = {"n": 0}


def _render(action) -> tuple[str, dict]:
    tool, target, value = action
    _counter["n"] += 1
    suffix = _counter["n"]
    if tool == "update_price":
        return tool, {"product_id": target, "delta_pct": round(value - suffix * 0.01, 3)}
    if tool == "update_stock":
        return tool, {"product_id": target, "delta": value}
    if tool == "create_coupon":
        return tool, {"code": f"{target}{suffix}", "discount_pct": value, "max_uses": 10}
    if tool == "get_product":
        return tool, {"product_id": target}
    return tool, {}


async def _run_sequence(actions) -> list[tuple[int, str, list[str]]]:
    """在全新栈上执行一个会话的动作序列，返回决策向量。"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        shop_app = create_shop_app(f"{tmp}/shop.db")
        async with LifespanManager(shop_app):
            app = create_app(
                Settings(shop_base_url="http://shop.test",
                         gateway_db_path=f"{tmp}/gateway.db"),
                shop_transport=httpx.ASGITransport(app=shop_app),
            )
            async with LifespanManager(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport,
                                             base_url="http://d") as c:
                    sid = (await c.post(
                        "/v1/sessions",
                        json={"agent_id": "ops_agent", "task_id": "det"},
                    )).json()["session_id"]
                    await c.post("/v1/tools/list_products",
                                 json={"session_id": sid, "args": {}})
                    out = []
                    for tool, args in actions:
                        r = await c.post(f"/v1/tools/{tool}", json={
                            "session_id": sid, "args": args})
                        body = r.json()
                        out.append((r.status_code,
                                    body.get("decision", ""),
                                    body.get("reasons", [])))
                    verdict = await c.get("/v1/audit/verify")
                    assert verdict.json()["ok"] is True
                    return out


@settings(max_examples=12, deadline=None)
@given(st.lists(_ACTIONS, min_size=3, max_size=10))
def test_same_sequence_same_decisions_on_two_fresh_stacks(actions):
    """同序列双栈执行：决策向量逐项一致（spec §21 B3 可复现性）。"""
    rendered = [_render(a) for a in actions]

    async def run():
        v1 = await _run_sequence(rendered)
        v2 = await _run_sequence(rendered)
        return v1, v2

    import asyncio

    v1, v2 = asyncio.run(run())
    assert v1 == v2
    # 序列里至少大部分动作真正进入判定（重放也有确定决策，但别全 404）
    assert all(status != 404 for status, _, _ in v1)
