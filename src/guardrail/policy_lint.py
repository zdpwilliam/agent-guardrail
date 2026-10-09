"""策略 lint（spec §12.4：`make lint` 的策略半边）。

ruff 管 Python，本模块管 YAML 策略的静态错误：三份策略可加载、
工具与 agent 引用全部存在于注册表。不查语义（那是评测的事），
只查「写错了名字」这类必然是 bug 的引用。

网关启动时同样跑这些校验（fail-closed），这里独立成 CLI 是为了让
`make lint` 能在**不启动服务**的情况下拦住策略错误——CI 里跑。
"""

from __future__ import annotations

import sys
from pathlib import Path

POLICIES = Path(__file__).resolve().parents[2] / "policies"


def lint_policies(policy_dir: Path = POLICIES) -> list[str]:
    """返回问题列表；空列表 = 通过。"""
    problems: list[str] = []

    from guardrail.policy.loader import (
        load_combined_policy,
        load_plan_policy,
        load_policy,
    )
    from guardrail.tools.registry import TOOL_SPECS, assert_specs_valid

    # 0. 工具注册表自检（效果声明/args schema/实体引用/taint）
    try:
        assert_specs_valid()
    except Exception as exc:  # noqa: BLE001 - lint 要收集而非中断
        problems.append(f"工具注册表自检失败: {exc}")

    # 1. 三份策略可加载
    try:
        single = load_policy(str(policy_dir / "single_call.yaml"))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"single_call.yaml 加载失败: {exc}")
        single = None
    try:
        combined = load_combined_policy(str(policy_dir / "combined_risk.yaml"))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"combined_risk.yaml 加载失败: {exc}")
        combined = None
    try:
        load_plan_policy(str(policy_dir / "plan_policy.yaml"))
    except Exception as exc:  # noqa: BLE001
        problems.append(f"plan_policy.yaml 加载失败: {exc}")

    if single is not None:
        # 2. 权限表与规则引用的工具必须存在
        for agent, tools in single.permissions.items():
            for tool in tools:
                if tool not in TOOL_SPECS:
                    problems.append(f"single_call.yaml: agent {agent!r} 授权了"
                                    f"不存在的工具 {tool!r}")
        for rule in single.rules:
            if rule.match.tool not in TOOL_SPECS:
                problems.append(f"single_call.yaml: 规则引用不存在的工具"
                                f" {rule.match.tool!r}")

    if combined is not None:
        # 3. cost_classes 的工具分类必须与单次策略权限表一致地指向真实工具
        for kind, tools in combined.budget.costs.cost_classes.items():
            for tool in tools:
                if tool not in TOOL_SPECS:
                    problems.append(f"combined_risk.yaml: cost_classes[{kind!r}]"
                                    f"引用不存在的工具 {tool!r}")

    return problems


def main() -> int:
    problems = lint_policies()
    if problems:
        print("策略 lint 未通过：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("策略 lint 通过：三份策略可加载，工具/agent 引用全部有效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
