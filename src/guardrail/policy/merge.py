"""跨会话合并与 combine 算子（spec §3.7）。

合并语义刻意保持与 spec 自己的设定一致：**Δ 按字段逐项相加**——单会话内
「两次降价 5% 就是 -10%」，跨会话合并沿用同一加法；跨 Agent 的「不可加」
只发生在 combine 环节（价格 × 折扣是乘法），那里由规则显式声明算子。
"""

from __future__ import annotations

import copy

from guardrail.models import EntityDelta

# combine 算子闭集。custom 由 lint 拒绝（M3 未实现），这里兜底抛错双保险。
_OPERATORS = {"add", "multiplicative", "max"}


def merge_entities(
    a: dict[str, EntityDelta], b: dict[str, EntityDelta]
) -> dict[str, EntityDelta]:
    """按实体 key 并集，字段逐项相加。不修改入参。"""
    merged = {k: copy.deepcopy(v) for k, v in a.items()}
    for key, delta in b.items():
        if key in merged:
            base = merged[key]
            base.price_delta_pct += delta.price_delta_pct
            base.stock_delta += delta.stock_delta
            base.coupon_rate_delta += delta.coupon_rate_delta
            base.email_delta += delta.email_delta
            if delta.last_updated_at > base.last_updated_at:
                base.last_updated_at = delta.last_updated_at
        else:
            merged[key] = copy.deepcopy(delta)
    return merged


def field_scalar(entities: dict[str, EntityDelta], field: str) -> float:
    """combine 的输入：该字段在全部实体上的总和。

    demo 里每个字段通常只落在一个实体上，精确。同一字段跨多实体的会话
    （比如一次改 5 个商品）是已知简化——记入 limitations，M7 复核。
    """
    return float(sum(getattr(e, field) for e in entities.values()))


def apply_combine(op: str, a: float, b: float) -> float:
    if op == "add":
        return a + b
    if op == "multiplicative":
        # 百分比语义：-8 表示 ×0.92。结果转回百分比偏移。
        return ((1 + a / 100) * (1 + b / 100) - 1) * 100
    if op == "max":
        # 字面最大值。对「负值越糟」的字段它取的是较轻的那个——这正是
        # spec §12.1 要演示的「用错算子 → 漏报」。不替策略作者兜底。
        return max(a, b)
    raise ValueError(f"未知或未实现的 combine 算子：{op}（custom 需注册命名函数，M3 未实现）")


def combine_values(entries: list[dict], entities: dict[str, EntityDelta]) -> float:
    """多条 combine 各算一个值，取最严重（最小值）。

    demo 只有一条；多条时「最严重 = 最小」对负向越界字段成立，正向字段
    （如累计退款额）将来加规则时需带 sign 语义，届时再扩。
    """
    if not entries:
        return 0.0
    values = [
        apply_combine(entry["op"], field_scalar(entities, entry["a"]),
                      field_scalar(entities, entry["b"]))
        for entry in entries
    ]
    return min(values)
