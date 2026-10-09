from __future__ import annotations

from typing import Any

from guardrail.models import (
    CouponSnapshot,
    EffectOp,
    EntityEmit,
    EntityRequire,
    OrderSnapshot,
    ProductSnapshot,
    RiskDeltaDecl,
    ToolSpec,
)

_ENTITY_ID = {"type": "string", "minLength": 1}
_ORDER_ID = {"type": "string", "minLength": 1}

# 参数契约是网关侧数据。additionalProperties 一律 False：模型多报一个字段
# 通常意味着它在编造，而服务端派生的字段（absolute_delta_cents）绝不能
# 由模型自己塞进来。
_ARGS_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_products": {
        "type": "object",
        "properties": {"category": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    },
    "get_product": {
        "type": "object",
        "properties": {"product_id": _ENTITY_ID},
        "required": ["product_id"],
        "additionalProperties": False,
    },
    "get_order": {
        "type": "object",
        "properties": {"order_id": _ORDER_ID},
        "required": ["order_id"],
        "additionalProperties": False,
    },
    "update_price": {
        "type": "object",
        "properties": {
            "product_id": _ENTITY_ID,
            "delta_pct": {"type": "number"},
        },
        "required": ["product_id", "delta_pct"],
        "additionalProperties": False,
    },
    "update_stock": {
        "type": "object",
        "properties": {
            "product_id": _ENTITY_ID,
            "delta": {"type": "integer"},
        },
        "required": ["product_id", "delta"],
        "additionalProperties": False,
    },
    "create_coupon": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "minLength": 1, "maxLength": 64},
            "discount_pct": {
                "type": "number",
                "exclusiveMinimum": 0,
                "exclusiveMaximum": 100,
            },
            "max_uses": {"type": "integer", "minimum": 1},
        },
        "required": ["code", "discount_pct", "max_uses"],
        "additionalProperties": False,
    },
    "create_order": {
        "type": "object",
        "properties": {
            "product_id": _ENTITY_ID,
            "qty": {"type": "integer", "minimum": 1},
            # 券可带可不带，显式 null 也合法（handler 用 args.get 读它）。
            "coupon_id": {"type": ["string", "null"], "minLength": 1},
        },
        "required": ["product_id", "qty"],
        "additionalProperties": False,
    },
    "refund_order": {
        "type": "object",
        "properties": {"order_id": _ORDER_ID},
        "required": ["order_id"],
        "additionalProperties": False,
    },
    "send_email": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "minLength": 3},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["to"],
        "additionalProperties": False,
    },
}

# 效果声明是网关侧数据，不是工具的代码。
# 工具实现（handlers.py）不含任何护栏逻辑；接入新工具时只改这里。
TOOL_SPECS: dict[str, ToolSpec] = {
    "list_products": ToolSpec(
        name="list_products",
        kind="read",
        args_schema=_ARGS_SCHEMAS["list_products"],
        emits=[EntityEmit(entity_type="product", path="products[].id")],
    ),
    "get_product": ToolSpec(
        name="get_product",
        kind="read",
        args_schema=_ARGS_SCHEMAS["get_product"],
        emits=[EntityEmit(entity_type="product", path="product.id")],
    ),
    "get_order": ToolSpec(
        name="get_order",
        kind="read",
        taint_source=True,
        args_schema=_ARGS_SCHEMAS["get_order"],
        emits=[EntityEmit(entity_type="order", path="order.id")],
        taint_categories=["pii"],
    ),
    "update_price": ToolSpec(
        name="update_price",
        kind="write",
        args_schema=_ARGS_SCHEMAS["update_price"],
        effects=[
            EffectOp(
                target="product:{args.product_id}",
                field="list_price_cents",
                op="add",
                value="{args.absolute_delta_cents}",
            )
        ],
        emits=[EntityEmit(entity_type="product", path="after.id")],
        requires=[EntityRequire(entity_type="product", arg="product_id")],
        risk_deltas=[
            RiskDeltaDecl(entity_type="product", entity_arg="product_id",
                          field="price_delta_pct", value_expr="args.delta_pct")
        ],
    ),
    "update_stock": ToolSpec(
        name="update_stock",
        kind="write",
        args_schema=_ARGS_SCHEMAS["update_stock"],
        effects=[
            EffectOp(
                target="product:{args.product_id}",
                field="stock",
                op="add",
                value="{args.delta}",
            )
        ],
        emits=[EntityEmit(entity_type="product", path="after.id")],
        requires=[EntityRequire(entity_type="product", arg="product_id")],
        risk_deltas=[
            RiskDeltaDecl(entity_type="product", entity_arg="product_id",
                          field="stock_delta", value_expr="args.delta")
        ],
    ),
    "create_coupon": ToolSpec(
        name="create_coupon",
        kind="write",
        args_schema=_ARGS_SCHEMAS["create_coupon"],
        effects=[
            EffectOp(
                target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}"
            )
        ],
        emits=[EntityEmit(entity_type="coupon", path="coupon_id")],
        # 发 8 折券 = 券贡献 -20：符号在声明处写明，提交段不变号。
        risk_deltas=[
            RiskDeltaDecl(entity_type="coupon", entity_arg="code",
                          field="coupon_rate_delta", value_expr="-args.discount_pct")
        ],
    ),
    "create_order": ToolSpec(
        name="create_order",
        kind="write",
        args_schema=_ARGS_SCHEMAS["create_order"],
        effects=[
            EffectOp(
                target="order:{args.order_id}", field="", op="append", value="{args.order_id}"
            ),
            # 真实商城（shop/store.py create_order）只做两件事：INSERT 订单，以及
            # 「带券时」UPDATE coupons SET used = used + 1。它不改 products.stock，
            # 也不在无券下单时碰任何券。optional=True 让 coupon_id 为 None 时这条
            # 效果退化为无操作，与商城逐字对齐。
            EffectOp(
                target="coupon:{args.coupon_id}",
                field="used",
                op="add",
                value=1,
                optional=True,
            ),
        ],
        emits=[
            EntityEmit(entity_type="order", path="order_id"),
            # 商品 id 是 provenance 门真正要看的东西：它藏在 order 载荷里，
            # 不在效果声明的 target 上，所以 requires 里必须显式再写一次。
            EntityEmit(entity_type="product", path="order.product_id"),
        ],
        requires=[
            EntityRequire(entity_type="product", arg="product_id"),
            EntityRequire(entity_type="coupon", arg="coupon_id", optional=True),
        ],
    ),
    "refund_order": ToolSpec(
        name="refund_order",
        kind="write",
        args_schema=_ARGS_SCHEMAS["refund_order"],
        effects=[
            EffectOp(target="order:{args.order_id}", field="status", op="set", value="refunded")
        ],
        emits=[EntityEmit(entity_type="order", path="order_id")],
        requires=[EntityRequire(entity_type="order", arg="order_id")],
    ),
    "send_email": ToolSpec(
        name="send_email",
        kind="write",
        taint_sink=True,
        args_schema=_ARGS_SCHEMAS["send_email"],
        # 邮箱粒度外发计数（corp 域 corp_email_fanout 规则用；电商规则不读此
        # 字段，对电商会话无影响）。负向计数与 §3.4①「越负越糟」语义一致。
        risk_deltas=[
            RiskDeltaDecl(entity_type="mailbox", entity_arg="to",
                          field="email_delta", value_expr="-1")
        ],
    ),
}

_VALID_OPS = {"add", "set", "mul", "append"}

# 实体类型 → 快照模型，用于校验 effect 的 field 是否真的存在于对应模型上。
_ENTITY_SNAPSHOTS: dict[str, type] = {
    "product": ProductSnapshot,
    "coupon": CouponSnapshot,
    "order": OrderSnapshot,
}

# append 只能新建 coupon / order；projection.py 会拒绝 product 上的 append。
_APPENDABLE_ENTITY_TYPES = {"coupon", "order"}

_PATH_HEAD = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _check_effects(name: str, spec: ToolSpec) -> None:
    for op in spec.effects:
        if op.op not in _VALID_OPS:
            raise RuntimeError(f"{name} 含非法 op: {op.op}")
        if ":" not in op.target:
            raise RuntimeError(f"{name} 的 target 缺少实体前缀: {op.target}")
        entity_type, _, entity_id = op.target.partition(":")
        if entity_type not in _ENTITY_SNAPSHOTS:
            raise RuntimeError(f"{name} 的 target 实体类型未知: {op.target}")
        if not entity_id:
            raise RuntimeError(f"{name} 的 target 实体 id 为空: {op.target}")
        if op.field and op.field not in _ENTITY_SNAPSHOTS[entity_type].model_fields:
            raise RuntimeError(
                f"{name} 的字段 {op.field!r} 不在 {entity_type} 快照模型上: {op.target}"
            )
        if op.op == "append" and entity_type not in _APPENDABLE_ENTITY_TYPES:
            raise RuntimeError(f"{name} 的 append 仅支持 coupon / order，收到 {op.target}")


def _check_args_schema(name: str, spec: ToolSpec) -> None:
    schema = spec.args_schema
    if not schema:
        raise RuntimeError(f"{name} 未声明 args_schema")
    if schema.get("type") != "object":
        raise RuntimeError(f"{name} 的 args_schema.type 必须是 object")
    # additionalProperties 不为 False 等于允许模型注入服务端派生字段，
    # 这是一个安全属性而不是风格偏好，所以放进启动期自检。
    if schema.get("additionalProperties") is not False:
        raise RuntimeError(f"{name} 的 args_schema 必须设置 additionalProperties: false")
    for required in schema.get("required", []):
        if required not in schema.get("properties", {}):
            raise RuntimeError(f"{name} 的 required 字段 {required!r} 未在 properties 中声明")


def _check_entity_refs(name: str, spec: ToolSpec) -> None:
    schema_required = set(spec.args_schema.get("required", []))
    for emit in spec.emits:
        head = emit.path.removesuffix("[]").split(".", 1)[0]
        if not head or head[0] not in _PATH_HEAD:
            raise RuntimeError(f"{name} 的 emits 路径非法: {emit.path}")
    for require in spec.requires:
        if require.arg not in spec.args_schema.get("properties", {}):
            raise RuntimeError(
                f"{name} 的 requires 引用了未声明的参数 {require.arg!r}（不在 args_schema 里）"
            )
        # 必填参数不可能是「可选依赖」：模型一定会下发它，provenance 就一定会
        # 被检查。标成 optional 的只该是那些 schema 里非必填的参数（如 coupon_id）。
        if require.optional and require.arg in schema_required:
            raise RuntimeError(
                f"{name} 的 requires 字段 {require.arg!r} 是必填参数，不能标为 optional"
            )


def _check_risk_deltas(name: str, spec: ToolSpec) -> None:
    from guardrail.policy.expr import ExpressionError, arg_names

    for decl in spec.risk_deltas:
        if decl.entity_arg not in spec.args_schema.get("properties", {}):
            raise RuntimeError(
                f"{name} 的 risk_deltas 引用了未声明的参数 {decl.entity_arg!r}"
            )
        try:
            referenced = arg_names(decl.value_expr)
        except ExpressionError as exc:
            raise RuntimeError(f"{name} 的 risk_deltas 表达式非法：{exc}") from exc
        unknown = sorted(referenced - set(spec.args_schema.get("properties", {})))
        if unknown:
            raise RuntimeError(
                f"{name} 的 risk_deltas 表达式引用了未声明的参数：{unknown}"
            )


def _check_taint(name: str, spec: ToolSpec) -> None:
    if spec.taint_categories and not spec.taint_source:
        raise RuntimeError(
            f"{name} 声明了 taint_categories 但未标记 taint_source——"
            "污点类别只配在污点源工具上"
        )


def assert_specs_valid(specs: dict[str, ToolSpec] | None = None) -> None:
    """启动期自检。工具清单有问题时进程应当直接起不来。"""
    if specs is None:
        specs = TOOL_SPECS
    # 9 个电商工具 + 7 个 corp 域工具（v0.3 起；send_email 两域复用）。
    # 新增域工具时同步更新此处与 policies/ 的 cost_classes / permissions。
    if len(specs) != 16:
        raise RuntimeError(f"工具数量应为 16，实际 {len(specs)}")
    for name, spec in specs.items():
        if spec.name != name:
            raise RuntimeError(f"工具键名与 spec.name 不一致: {name} != {spec.name}")
        # 效果先校验：它承载的是「这个工具会改什么」，错了会直接让投影失真；
        # 参数契约与实体引用排在其后。
        _check_effects(name, spec)
        _check_args_schema(name, spec)
        _check_entity_refs(name, spec)
        _check_risk_deltas(name, spec)
        _check_taint(name, spec)


# corp 域（v0.3）：第二领域的工具注册——域隔离靠策略权限表，不靠注册表分裂。
# 放在模块尾：corp.py 反向导入本模块的 TOOL_SPECS 做 update，必须等定义完整。
from guardrail.domains import corp as _corp  # noqa: E402,F401,I001 - 导入即注册（必须在 TOOL_SPECS 之后）
