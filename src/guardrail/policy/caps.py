from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from guardrail.models import OrderSnapshot, ShadowState
from guardrail.policy.single import SingleCallPolicy
from guardrail.projection import ProjectionError
from guardrail.shadow_loader import ShadowLoadError, load_shadow, project_call

CAP_ERROR_PREFIX = "结果态上限"


class CapsEvaluationError(Exception):
    """结果层上限无法求值。调用方必须 fail-closed 拒绝。"""


# 可读字段的闭集，与 policy/lint.py 的 CAP_FIELDS 一一对应。
# 刻意不把表达式交给策略文件：能读任意字段就等于把求值沙箱的口子开在配置层。
_CAP_READERS: dict[str, Callable[[OrderSnapshot], int]] = {
    "total_amount_cents": lambda order: order.unit_price_cents * order.qty,
}


def _new_order(before_ids: set[str], after: Any) -> OrderSnapshot:  # noqa: ANN401
    """取出本次调用新建的订单。

    刻意要求「恰好一个」：0 个说明投影没生效，多个说明这次调用建了不止一张
    单——两种情况都意味着读到的金额不可信，必须拒绝而不是猜一个。
    """
    new_orders = [o for oid, o in after.orders.items() if oid not in before_ids]
    if len(new_orders) != 1:
        raise CapsEvaluationError(
            f"结果态上限无法求值：本次调用新建了 {len(new_orders)} 张订单，预期恰好 1 张"
        )
    return new_orders[0]


async def evaluate_result_caps(
    *,
    tool: str,
    args: dict[str, Any],
    policy: SingleCallPolicy,
    shop: httpx.AsyncClient,
) -> tuple[str, str] | None:
    """对一次调用做结果层上限校验。返回 `(规则 id, 原因)` 或 `None`。

    金额只从投影结果态读（spec §6.3）：模型可以谎报 args 里的任何数字，
    谎报不了投影结果。这也意味着**每个结果层规则都要付一次装载 + 投影的代价**，
    所以不要把所有规则都升级成结果层——只有「金额 / 总量 / 终态」这类约束才值得。
    """
    rules = [r for r in policy.rules_for(tool) if r.cap_field is not None]
    if not rules:
        return None

    try:
        before = await load_shadow(shop, [(tool, args)])
        after = await project_call(shop, tool, args)
    except (ShadowLoadError, ProjectionError) as exc:
        # 投影不出来就不能声称「没超限」。这是 fail-closed 最容易漏掉的一处：
        # 把它当成「通过」等于给了一条绕过上限的路径。
        raise CapsEvaluationError(
            f"{CAP_ERROR_PREFIX}无法求值，已按 fail-closed 拒绝：{exc}"
        ) from exc

    return evaluate_result_caps_for_projection(tool, policy, before, after)


def evaluate_result_caps_for_projection(
    tool: str,
    policy: SingleCallPolicy,
    before: ShadowState,
    after: ShadowState,
) -> tuple[str, str] | None:
    """对已经完成投影的一步调用求结果态上限。

    计划提交链需要沿影子状态逐步验证，不能重新从真实商城装载单步影子，
    否则前序计划的改价/库存效果不会体现在当前步的终态里。
    """
    rules = [r for r in policy.rules_for(tool) if r.cap_field is not None]
    if not rules:
        return None

    for rule in rules:
        reader = _CAP_READERS[rule.cap_field]
        amount = reader(_new_order(set(before.orders), after))
        assert rule.max is not None
        if amount > rule.max:
            return rule.id, rule.message
    return None
