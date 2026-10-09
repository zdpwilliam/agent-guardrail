from __future__ import annotations

from guardrail.models import ToolSpec
from guardrail.policy.combined import DELTA_FIELDS, CombinedPolicy
from guardrail.policy.expr import ExpressionError, arg_names
from guardrail.policy.single import PolicyError, SingleCallPolicy

# 结果层可读字段的闭集。新增一个字段必须在这里同时给出「从哪个快照、怎么算」，
# 而不是让策略文件自己写表达式——那等于把求值沙箱开一个口子。
CAP_FIELDS: tuple[str, ...] = ("total_amount_cents",)


def lint_policy(policy: SingleCallPolicy, tools: dict[str, ToolSpec]) -> None:
    """启动期策略自检。一次性报出全部问题。

    逐个报错、逐个修的体验在启动期是灾难：改一个 YAML 要重启三次进程。
    """
    problems: list[str] = []

    seen: set[str] = set()
    for rule in policy.rules:
        if rule.id in seen:
            problems.append(f"规则 id 重复：{rule.id}")
        seen.add(rule.id)

        spec = tools.get(rule.match.tool)
        if spec is None:
            problems.append(f"规则 {rule.id!r} 引用了未知的工具：{rule.match.tool}")
            continue

        if rule.deny_if is not None:
            problems.extend(_lint_expression(rule.id, rule.deny_if, spec))
        elif rule.cap_field is not None and rule.cap_field not in CAP_FIELDS:
            problems.append(
                f"规则 {rule.id!r} 的 cap_field {rule.cap_field!r} 不在可读字段集 "
                f"{list(CAP_FIELDS)} 内"
            )

    problems.extend(_lint_permissions(policy, tools))
    problems.extend(_lint_tool_coverage(policy, tools))

    if problems:
        detail = "\n".join(f"  - {p}" for p in problems)
        raise PolicyError(f"策略文件未通过自检：\n{detail}")


def _lint_expression(rule_id: str, expression: str, spec: ToolSpec) -> list[str]:
    try:
        names = arg_names(expression)
    except ExpressionError as exc:
        return [f"规则 {rule_id!r} 的表达式非法：{exc}"]

    declared = set(spec.args_schema.get("properties", {}))
    unknown = sorted(names - declared)
    if unknown:
        return [f"规则 {rule_id!r} 的表达式引用了 {spec.name} 未声明的参数：{unknown}"]
    return []


def _lint_permissions(policy: SingleCallPolicy, tools: dict[str, ToolSpec]) -> list[str]:
    problems: list[str] = []
    for agent_id, granted in policy.permissions.items():
        if not granted:
            problems.append(f"agent {agent_id!r} 的权限列表是空列表")
        for tool in granted:
            if tool not in tools:
                problems.append(f"agent {agent_id!r} 被授予了未知的工具：{tool}")
    return problems


def _lint_tool_coverage(policy: SingleCallPolicy, tools: dict[str, ToolSpec]) -> list[str]:
    # covers 非空时只检查声明覆盖的工具（多域：corp 策略不管电商工具）。
    if policy.covers:
        tools = {t: spec for t, spec in tools.items() if t in policy.covers}
    granted = {tool for tool_list in policy.permissions.values() for tool in tool_list}
    ungranted = sorted(set(tools) - granted)
    if ungranted:
        return [f"以下工具没有任何 agent 授权：{ungranted}"]
    return []


# ---------- 组合风险策略自检（spec §3） ----------


def lint_combined_policy(policy: CombinedPolicy, tools: dict[str, ToolSpec]) -> None:
    """启动期组合策略自检。同样一次性报出全部问题。"""
    problems: list[str] = []

    seen: set[str] = set()
    for rule in policy.rules:
        if rule.id in seen:
            problems.append(f"规则 id 重复：{rule.id}")
        seen.add(rule.id)

        if rule.type == "cumulative":
            if rule.field not in DELTA_FIELDS:  # pragma: no cover - Literal 已约束
                problems.append(f"规则 {rule.id!r} 引用了未知的 Δ 字段：{rule.field}")
            if rule.warn_at is None and rule.deny_at is None:
                problems.append(f"规则 {rule.id!r} 至少声明 warn_at 或 deny_at 之一")

        elif rule.type == "sequence":
            if not rule.steps:
                problems.append(f"规则 {rule.id!r} 的 steps 为空")
            for step in rule.steps:
                spec = tools.get(step.tool)
                if spec is None:
                    problems.append(f"规则 {rule.id!r} 引用了未知的工具：{step.tool}")
                    continue
                problems.extend(
                    _lint_expression_args(rule.id, step.where, spec)
                    if step.where
                    else []
                )
            if rule.max_gap < 0:
                problems.append(f"规则 {rule.id!r} 的 max_gap 不能为负")

        elif rule.type == "taint":
            for source in rule.sources:
                spec = tools.get(source)
                if spec is None:
                    problems.append(f"规则 {rule.id!r} 引用了未知的污点源：{source}")
                elif not spec.taint_source:
                    problems.append(
                        f"规则 {rule.id!r} 的污点源 {source} 未在工具清单里标记 taint_source"
                    )
            for sink in rule.sinks:
                spec = tools.get(sink)
                if spec is None:
                    problems.append(f"规则 {rule.id!r} 引用了未知的污点汇：{sink}")
                    continue
                problems.extend(
                    _lint_expression_args(rule.id, rule.sink_condition, spec)
                    if rule.sink_condition
                    else []
                )

        elif rule.type == "cross_agent":
            if not rule.contributors:
                problems.append(f"规则 {rule.id!r} 的 contributors 为空")
            if not rule.combine:
                problems.append(f"规则 {rule.id!r} 的 combine 为空")
            for entry in rule.combine:
                for side in (entry.a, entry.b):
                    if side not in DELTA_FIELDS:
                        problems.append(
                            f"规则 {rule.id!r} 的 combine 引用了未知的 Δ 字段：{side}"
                        )
                if entry.op == "custom":
                    problems.append(
                        f"规则 {rule.id!r} 使用了 custom 算子：M3 未实现注册命名函数"
                        "（那是一个新的逃逸面），请用 add / multiplicative / max"
                    )

    problems.extend(_lint_budget(policy, tools))

    if problems:
        detail = "\n".join(f"  - {p}" for p in problems)
        raise PolicyError(f"组合策略未通过自检：\n{detail}")


def _lint_expression_args(
    rule_id: str, expression: str, spec: ToolSpec
) -> list[str]:
    """序列步的 where / 污点的 sink_condition：可解析，且 args 引用都在该工具契约里。"""
    try:
        names = arg_names(expression)
    except ExpressionError as exc:
        return [f"规则 {rule_id!r} 的表达式非法：{exc}"]
    declared = set(spec.args_schema.get("properties", {}))
    unknown = sorted(names - declared)
    if unknown:
        return [f"规则 {rule_id!r} 的表达式引用了 {spec.name} 未声明的参数：{unknown}"]
    return []


def _lint_budget(policy: CombinedPolicy, tools: dict[str, ToolSpec]) -> list[str]:
    problems: list[str] = []
    if policy.budget.initial <= 0:
        problems.append(f"budget.initial 必须为正，收到 {policy.budget.initial}")

    counts: dict[str, int] = {}
    # covers 非空时只检查声明覆盖的工具（多域：corp 组合策略不管电商工具）。
    scope = (set(policy.covers) if policy.covers else set(tools))
    for class_name, class_tools in policy.budget.costs.cost_classes.items():
        for tool in class_tools:
            if tool not in tools:
                problems.append(f"成本分类 {class_name!r} 引用了未知的工具：{tool}")
            counts[tool] = counts.get(tool, 0) + 1
    for tool in scope:
        n = counts.get(tool, 0)
        if n == 0:
            problems.append(f"工具 {tool} 未被任何成本分类覆盖")
        elif n > 1:
            problems.append(f"工具 {tool} 出现在 {n} 个成本分类里，只能有一个")

    t = policy.budget.thresholds
    if not (t.deny_below < t.ask_at_or_below < t.flag_at_or_below):
        problems.append(
            f"预算阈值必须严格单调递增：deny_below({t.deny_below}) < "
            f"ask_at_or_below({t.ask_at_or_below}) < flag_at_or_below({t.flag_at_or_below})"
        )
    return problems
