from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from guardrail.plans import PlanPolicy
from guardrail.policy.combined import CombinedPolicy
from guardrail.policy.lint import lint_combined_policy, lint_policy
from guardrail.policy.single import PolicyError, SingleCallPolicy
from guardrail.tools.registry import TOOL_SPECS


def load_policy(path: str) -> SingleCallPolicy:
    """加载并自检单次策略。任何环节失败都抛 PolicyError，调用方必须拒绝启动。"""
    file = Path(path)
    if not file.is_file():
        raise PolicyError(f"策略文件不存在：{path}")

    try:
        raw: Any = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError(f"策略文件无法解析：{exc}") from exc

    if not isinstance(raw, dict):
        raise PolicyError(f"策略文件顶层必须是映射，收到 {type(raw).__name__}")

    try:
        policy = SingleCallPolicy.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(f"策略文件结构非法：{exc}") from exc

    lint_policy(policy, TOOL_SPECS)
    return policy


def load_combined_policy(path: str) -> CombinedPolicy:
    """加载并自检组合风险策略。任何环节失败都抛 PolicyError，调用方必须拒绝启动。

    与 load_policy 刻意做成两个函数而不是一个「加载目录」：两份策略的失败原因
    要能各自定位——运维看到「策略文件不存在：/path/single_call.yaml」和
    「/path/combined_risk.yaml」时需要知道改哪一份。
    """
    file = Path(path)
    if not file.is_file():
        raise PolicyError(f"策略文件不存在：{path}")

    try:
        raw: Any = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError(f"策略文件无法解析：{exc}") from exc

    if not isinstance(raw, dict):
        raise PolicyError(f"策略文件顶层必须是映射，收到 {type(raw).__name__}")

    try:
        policy = CombinedPolicy.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(f"策略文件结构非法：{exc}") from exc

    lint_combined_policy(policy, TOOL_SPECS)
    return policy


def load_plan_policy(path: str) -> PlanPolicy:
    """加载分级系数。缺字段用默认值——分级参数不是安全边界（阈值本身才是），
    但文件必须可解析。"""
    file = Path(path)
    if not file.is_file():
        raise PolicyError(f"策略文件不存在：{path}")
    try:
        raw: Any = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError(f"策略文件无法解析：{exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError(f"策略文件顶层必须是映射，收到 {type(raw).__name__}")
    try:
        return PlanPolicy.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(f"策略文件结构非法：{exc}") from exc
