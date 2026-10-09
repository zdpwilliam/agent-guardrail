from typing import Any, Literal

from pydantic import BaseModel, Field


class ProductSnapshot(BaseModel):
    product_id: str
    cost_price_cents: int
    list_price_cents: int
    stock: int


class CouponSnapshot(BaseModel):
    coupon_id: str
    code: str
    discount_pct: float
    max_uses: int
    used: int


class OrderSnapshot(BaseModel):
    order_id: str
    product_id: str
    qty: int
    unit_price_cents: int
    coupon_id: str | None = None
    status: str = "created"


class ShadowState(BaseModel):
    """网关侧影子领域模型。投影只改它，永不碰真实商城。"""

    products: dict[str, ProductSnapshot] = Field(default_factory=dict)
    coupons: dict[str, CouponSnapshot] = Field(default_factory=dict)
    orders: dict[str, OrderSnapshot] = Field(default_factory=dict)
    touched: set[str] = Field(default_factory=set)

    def clone(self) -> "ShadowState":
        return ShadowState(
            products={k: v.model_copy(deep=True) for k, v in self.products.items()},
            coupons={k: v.model_copy(deep=True) for k, v in self.coupons.items()},
            orders={k: v.model_copy(deep=True) for k, v in self.orders.items()},
            touched=set(self.touched),
        )


class BusinessMetrics(BaseModel):
    gross_margin_pct_before: float
    gross_margin_pct_after: float
    avg_price_cents_before: int
    avg_price_cents_after: int
    affected_product_count: int
    cash_impact_cents: int


class ProjectedState(BaseModel):
    entities: dict[str, ProductSnapshot] = Field(default_factory=dict)
    metrics: BusinessMetrics
    triggered_rules: list[str] = Field(default_factory=list)


class EffectOp(BaseModel):
    """效果声明的一条操作。value 是模板串，形如 '{args.delta_pct}'。"""

    target: str
    field: str
    op: Literal["add", "set", "mul", "append"]
    value: Any = None
    # 可选引用：目标里的实体 id 模板变量取值为 None 时，这条效果是无操作
    # （如 create_order 未带 coupon_id 就不该动任何券）。默认 False 保持
    # fail-closed——缺失/未指名的实体 id 照旧报错。
    optional: bool = False


class EntityEmit(BaseModel):
    """工具返回值里实体 id 的位置。

    path 语法：`coupon.id`、`products[].id`、`order_id`。刻意用声明式路径，
    而不是「递归扫返回值里所有像 id 的字符串」——扫描会把商品名、邮箱域名
    也登记成 provenance，那道门就形同虚设了。
    """

    entity_type: Literal["product", "coupon", "order", "file", "mailbox"]
    path: str


class EntityRequire(BaseModel):
    """写操作必须已被本会话读到过的实体参数（spec §8）。"""

    entity_type: Literal["product", "coupon", "order", "file", "mailbox"]
    arg: str
    optional: bool = False


class EntityDelta(BaseModel):
    """对某个实体的累积影响（spec §3.2 的 Δ 分量）。

    刻意用「会话级可加」语义（spec §3.7 自己的设定：两次降价 5% 就是 -10%），
    实体 key 只做归组，不做跨实体校验。多实体聚合的精化留给 M7 评测复核。
    """

    entity_key: str  # 如 "product:p-iphone"、"mailbox:leaker@evil.com"
    price_delta_pct: float = 0.0
    stock_delta: int = 0
    coupon_rate_delta: float = 0.0
    # corp 域（v0.3）：外发邮件累计（负向计数，与价格同用「越负越糟」语义）
    email_delta: int = 0
    last_updated_at: str


class ActionRecord(BaseModel):
    """有序动作序列 A 的一条记录。只存判定需要的最小信息：tool + args。"""

    seq: int
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    timestamp: str


class SessionState(BaseModel):
    """会话风险状态（spec §3.6 的三元组 (Δ, T, A) 物化形态）。

    与 spec §3.2 的偏离（M3 实现回写，见计划 Task 2）：agent_id / task_id /
    expires_at 留在 SessionRecord（身份行），不进状态 JSON——同一事实存两处
    是分裂的温床。plan_id 属于计划生命周期（M4）再加。
    """

    session_id: str
    entities: dict[str, EntityDelta] = Field(default_factory=dict)
    taint: set[str] = Field(default_factory=set)
    actions: list[ActionRecord] = Field(default_factory=list)
    risk_budget: float = 1.0
    # allow_with_flag 的会话标记（spec §3.3 阶梯第 3 段）。
    flagged: bool = False


class RiskDeltaDecl(BaseModel):
    """工具对会话风险三元组 Δ 的贡献声明（spec §3.6，网关侧数据）。

    value_expr 用受限表达式求值器（policy/expr.py）对 args 求值——沙箱免费复用，
    且符号在声明处写明（发 8 折券 = coupon_rate_delta -20），提交段不做谜之变号。
    entity_key 由 `f"{entity_type}:{args[entity_arg]}"` 合成。
    """

    entity_type: Literal["product", "coupon", "order", "file", "mailbox"]
    entity_arg: str
    field: Literal["price_delta_pct", "stock_delta", "coupon_rate_delta", "email_delta"]
    value_expr: str


class ToolSpec(BaseModel):
    name: str
    kind: Literal["read", "write"]
    effects: list[EffectOp] = Field(default_factory=list)
    taint_source: bool = False
    taint_sink: bool = False
    # 参数契约（JSON Schema）。放在网关侧的工具清单里而不是工具自己的代码里，
    # 理由与效果声明相同（spec §4.3）：接入新工具只改一个文件，工具侧零改动。
    args_schema: dict[str, Any] = Field(default_factory=dict)
    # 执行成功后从返回值里登记哪些实体（provenance 的「发」）。
    emits: list[EntityEmit] = Field(default_factory=list)
    # 执行前必须已被本会话读到过哪些实体（provenance 的「收」）。
    requires: list[EntityRequire] = Field(default_factory=list)
    # 生效后对风险三元组 Δ 的贡献声明（spec §3.6）。
    risk_deltas: list[RiskDeltaDecl] = Field(default_factory=list)
    # 生效后写入会话污点集合 T 的类别（仅 taint_source 工具，自检强制）。
    taint_categories: list[str] = Field(default_factory=list)


def _margin_pct(products: dict[str, ProductSnapshot]) -> float:
    revenue = sum(p.list_price_cents for p in products.values())
    cost = sum(p.cost_price_cents for p in products.values())
    if revenue == 0:
        return 0.0
    return round((revenue - cost) / revenue * 100, 2)


def _avg_price(products: dict[str, ProductSnapshot]) -> int:
    if not products:
        return 0
    return round(sum(p.list_price_cents for p in products.values()) / len(products))


def compute_metrics(before: ShadowState, after: ShadowState) -> BusinessMetrics:
    """从前后两个影子状态算出业务指标。只比较 before 中出现过的商品。"""
    affected = [
        pid
        for pid in before.products
        if pid in after.products
        and (
            before.products[pid].list_price_cents != after.products[pid].list_price_cents
            or before.products[pid].stock != after.products[pid].stock
        )
    ]
    new_orders = [o for oid, o in after.orders.items() if oid not in before.orders]
    cash_impact = sum(o.unit_price_cents * o.qty for o in new_orders)

    return BusinessMetrics(
        gross_margin_pct_before=_margin_pct(before.products),
        gross_margin_pct_after=_margin_pct(after.products),
        avg_price_cents_before=_avg_price(before.products),
        avg_price_cents_after=_avg_price(after.products),
        affected_product_count=len(affected),
        cash_impact_cents=cash_impact,
    )
