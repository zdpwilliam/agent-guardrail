import httpx

from guardrail.clock import now_iso
from guardrail.policy.engine import STEP_ORDER, evaluate_single_call, is_session_valid
from guardrail.policy.loader import load_policy
from guardrail.policy.single import SingleCallPolicy
from guardrail.protocols import SessionRecord

POLICY = load_policy("policies/single_call.yaml")

# 判定链的前 6 步全是语法层，不碰商城。Task 9 会把第 7 步（结果态上限）接上，
# 而仓库策略里 create_order 带一条 cap 规则——那会让本文件的 create_order 用例
# 在 Task 9 之后突然需要真实商城。所以这里显式用一份「剥掉结果层规则」的策略，
# 把本文件的作用域钉死在语法层：第 7 步由 tests/test_caps.py 负责。
SYNTAX_ONLY_POLICY = SingleCallPolicy(
    version=POLICY.version,
    permissions=POLICY.permissions,
    rules=[r for r in POLICY.rules if r.cap_field is None],
)


def _unreachable_shop(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"语法层判定不该发起商城调用，却请求了 {request.url}")


class FakeProvenance:
    """只实现判定链用到的两个方法。真实实现有 SQLite 往返，单测里不需要。"""

    def __init__(self, known: set[tuple[str, str, str]] | None = None) -> None:
        self.known = known or set()

    async def register(self, session_id, refs) -> None:
        for entity_type, entity_id in refs:
            self.known.add((session_id, entity_type, entity_id))

    async def contains(self, session_id, entity_type, entity_id) -> bool:
        return (session_id, entity_type, entity_id) in self.known


def _record(agent_id: str = "ops_agent", expires_at: str | None = None) -> SessionRecord:
    return SessionRecord(
        session_id="s-1",
        agent_id=agent_id,
        task_id=None,
        created_at=now_iso(),
        expires_at=expires_at or "2999-01-01T00:00:00+00:00",
    )


async def _evaluate(record, tool, args, prov=None, policy=SYNTAX_ONLY_POLICY):
    # shop 用一个「任何请求都报错」的 transport：如果某条本该纯语法的用例
    # 意外走到第 7 步，这里会立刻炸，而不是静默发一个注定失败的请求。
    async with httpx.AsyncClient(transport=httpx.MockTransport(_unreachable_shop)) as shop:
        return await evaluate_single_call(
            record=record,
            tool=tool,
            args=args,
            policy=policy,
            provenance_store=prov or FakeProvenance(),
            shop=shop,
        )


# ---------- 判定链顺序本身 ----------


def test_step_order_is_the_documented_one():
    assert STEP_ORDER == (
        "session",
        "tool_known",
        "provenance",
        "args_schema",
        "permission",
        "single_threshold",
        "resulting_state_cap",
    )


# ---------- 第 1 步：会话 ----------


def test_session_valid_when_not_expired():
    assert is_session_valid(_record(), now_iso())


def test_session_invalid_when_expired():
    assert not is_session_valid(_record(expires_at="2000-01-01T00:00:00+00:00"), now_iso())


def test_session_invalid_when_expiry_unparsable():
    # 解析不了就当过期：宁可多拒一次，也不能让一个坏时间戳换来一个永久会话。
    assert not is_session_valid(_record(expires_at="not-a-timestamp"), now_iso())


def test_session_invalid_when_expiry_has_no_timezone():
    assert not is_session_valid(_record(expires_at="2999-01-01T00:00:00"), now_iso())


async def test_expired_session_denied():
    verdict = await _evaluate(
        _record(expires_at="2000-01-01T00:00:00+00:00"), "list_products", {}
    )
    assert verdict.decision == "deny"
    assert "过期" in verdict.reasons[0]


# ---------- 第 2 步：工具白名单 ----------


async def test_unknown_tool_denied():
    verdict = await _evaluate(_record(), "teleport", {})
    assert verdict.decision == "deny"
    assert "teleport" in verdict.reasons[0]


# ---------- 第 3 步：provenance ----------


async def test_write_on_unseen_entity_denied():
    verdict = await _evaluate(_record(), "update_price", {"product_id": "p-x", "delta_pct": -1})
    assert verdict.decision == "deny"
    assert "Provenance" in verdict.reasons[0]


async def test_write_on_seen_entity_passes_provenance():
    prov = FakeProvenance({("s-1", "product", "p-x")})
    verdict = await _evaluate(
        _record(), "update_price", {"product_id": "p-x", "delta_pct": -1.0}, prov
    )
    assert verdict.decision == "allow"


# ---------- 第 4 步：参数契约 ----------


async def test_invalid_args_is_a_distinct_kind():
    verdict = await _evaluate(_record(), "get_product", {})
    assert verdict.decision == "deny"
    # 畸形参数是调用方的错（400），不是策略违规（403）——Agent 靠这个区分
    # 「我参数写错了」和「我不被允许」。
    assert verdict.kind == "invalid_args"


async def test_invalid_args_message_names_tool_and_field():
    verdict = await _evaluate(_record(), "get_product", {})
    text = " ".join(verdict.reasons)
    assert "get_product" in text
    assert "product_id" in text


# ---------- 第 5 步：权限 ----------


async def test_agent_without_permission_denied():
    # 先把 p 登记进 provenance，否则会在第 3 步就被拦下，测不到第 5 步。
    prov = FakeProvenance({("s-1", "product", "p")})
    verdict = await _evaluate(
        _record(agent_id="risk_auditor"),
        "update_price",
        {"product_id": "p", "delta_pct": -1.0},
        prov,
    )
    assert verdict.decision == "deny"
    assert "无权" in verdict.reasons[0]


async def test_unlisted_agent_denied_everything():
    verdict = await _evaluate(_record(agent_id="ghost"), "list_products", {})
    assert verdict.decision == "deny"


async def test_pricing_agent_cannot_create_coupon():
    verdict = await _evaluate(
        _record(agent_id="pricing_agent"),
        "create_coupon",
        {"code": "X", "discount_pct": 20.0, "max_uses": 5},
    )
    assert verdict.decision == "deny"


async def test_permission_is_checked_after_provenance():
    # 顺序有意义：先看「这个工具动不动得了别的实体」，再看「你能不能动」。
    # 两条都失败时，provenance 的原因先出现——它更接近攻击形态。
    verdict = await _evaluate(
        _record(agent_id="risk_auditor"), "update_price", {"product_id": "p-x", "delta_pct": -1.0}
    )
    assert "Provenance" in verdict.reasons[0]


# ---------- 第 6 步：单次阈值 ----------


async def test_single_price_cut_over_10_denied():
    prov = FakeProvenance({("s-1", "product", "p-x")})
    verdict = await _evaluate(
        _record(), "update_price", {"product_id": "p-x", "delta_pct": -50.0}, prov
    )
    assert verdict.decision == "deny"
    assert verdict.rule_id == "max_single_price_cut"
    assert "10%" in verdict.reasons[0]


async def test_single_price_cut_exactly_10_allowed():
    prov = FakeProvenance({("s-1", "product", "p-x")})
    verdict = await _evaluate(
        _record(), "update_price", {"product_id": "p-x", "delta_pct": -10.0}, prov
    )
    assert verdict.decision == "allow"


async def test_price_increase_is_also_bounded():
    prov = FakeProvenance({("s-1", "product", "p-x")})
    verdict = await _evaluate(
        _record(), "update_price", {"product_id": "p-x", "delta_pct": 50.0}, prov
    )
    assert verdict.decision == "deny"


async def test_stock_delta_bounded():
    prov = FakeProvenance({("s-1", "product", "p-x")})
    ok = await _evaluate(_record(), "update_stock", {"product_id": "p-x", "delta": -500}, prov)
    too_far = await _evaluate(
        _record(), "update_stock", {"product_id": "p-x", "delta": -501}, prov
    )
    assert ok.decision == "allow"
    assert too_far.decision == "deny"
    assert too_far.rule_id == "max_single_stock_delta"


async def test_coupon_discount_threshold_leaves_room_for_combined_risk():
    args = {"code": "X", "discount_pct": 60.0, "max_uses": 5}
    assert (await _evaluate(_record(), "create_coupon", args)).decision == "allow"
    args = {"code": "X", "discount_pct": 90.0, "max_uses": 5}
    verdict = await _evaluate(_record(), "create_coupon", args)
    assert verdict.decision == "deny"
    assert verdict.rule_id == "max_coupon_discount"


async def test_discounted_bulk_order_denied():
    # 刻意用小额商品 p-tshirt-s：这条只验语法层第 6 步，不希望第 7 步的
    # 金额上限（50000 分）掺进来。p-iphone 一件就 599900 分，会被第 7 步拦掉，
    # 那样测的就不是本条规则了。
    prov = FakeProvenance({("s-1", "product", "p-tshirt-s"), ("s-1", "coupon", "c-1")})
    ok = await _evaluate(
        _record(), "create_order", {"product_id": "p-tshirt-s", "qty": 49, "coupon_id": "c-1"}, prov
    )
    too_many = await _evaluate(
        _record(), "create_order", {"product_id": "p-tshirt-s", "qty": 50, "coupon_id": "c-1"}, prov
    )
    assert ok.decision == "allow"
    assert too_many.decision == "deny"
    assert too_many.rule_id == "no_discounted_bulk_order"


async def test_bulk_order_without_coupon_is_allowed_by_that_rule():
    prov = FakeProvenance({("s-1", "product", "p-tshirt-s")})
    verdict = await _evaluate(
        _record(), "create_order", {"product_id": "p-tshirt-s", "qty": 500}, prov
    )
    assert verdict.decision == "allow"


# ---------- fail-closed ----------


async def test_expression_failure_denies(monkeypatch):
    import guardrail.policy.engine as engine

    def boom(_expression, _args):
        from guardrail.policy.expr import ExpressionError

        raise ExpressionError("模拟求值失败")

    monkeypatch.setattr(engine, "evaluate", boom)
    prov = FakeProvenance({("s-1", "product", "p-x")})
    verdict = await _evaluate(
        _record(), "update_price", {"product_id": "p-x", "delta_pct": -50.0}, prov
    )
    assert verdict.decision == "deny"
    assert "求值失败" in " ".join(verdict.reasons)


async def test_first_matching_rule_wins():
    prov = FakeProvenance({("s-1", "product", "p-x")})
    verdict = await _evaluate(
        _record(), "update_price", {"product_id": "p-x", "delta_pct": -99.0}, prov
    )
    assert verdict.rule_id == "max_single_price_cut"
    assert len(verdict.reasons) == 1
