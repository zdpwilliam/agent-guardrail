"""风险预算：成本分类与决策阶梯合成（spec §3.3）。

关键语义（本计划拍板，spec 回写见 Task 12）：
- **决策读扣减前预算**：阶梯看的是「这次调用到来时」的预算。扣减发生在
  生效提交段（只记生效调用）——扣减后判定会让 ASK 提前一次触发。
- **cost 与 decision 解耦**：ASK 时也把 cost 算好带回去，批准后执行时才扣；
  deny 时不扣（组合 deny 规则是独立后盾，没生效的调用不消耗预算）。
"""

from __future__ import annotations

from typing import Literal

from guardrail.policy.combined import CombinedPolicy

BudgetDecision = Literal["allow", "allow_with_flag", "ask", "deny"]


def classify_cost(policy: CombinedPolicy, tool: str) -> float:
    """按策略的成本分类表取基础成本。未分类工具直接炸——lint 已保证全覆盖，
    这里是第二道闸：新增工具忘配策略必须炸在第一次调用，而不是静默按 0 放行。"""
    costs = policy.budget.costs
    for class_name, tools in costs.cost_classes.items():
        if tool in tools:
            if class_name == "sensitive_write":
                return costs.sensitive_write
            if class_name == "normal_write":
                return costs.normal_write
            if class_name == "read":
                return 0.0
            raise ValueError(f"未知成本分类：{class_name}")
    raise RuntimeError(f"工具 {tool!r} 未被任何成本分类覆盖（策略文件缺配置）")


def synth_decision(
    budget: float, base_cost: float, warn_count: int, cfg: object
) -> tuple[BudgetDecision, float]:
    """决策阶梯（自上而下首次命中）+ 本次调用成本。

    cost = 基础成本 + warn 命中数 × 附加成本。阶梯判定用**扣减前**预算。
    """
    thresholds = cfg.thresholds  # type: ignore[attr-defined]
    cost = cfg.costs  # type: ignore[attr-defined]
    total = base_cost + warn_count * cost.warn_surcharge

    if budget < thresholds.deny_below:
        return "deny", total
    if budget <= thresholds.ask_at_or_below:
        return "ask", total
    if budget <= thresholds.flag_at_or_below:
        return "allow_with_flag", total
    return "allow", total
