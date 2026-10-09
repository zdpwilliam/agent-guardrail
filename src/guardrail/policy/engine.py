from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from guardrail.clock import now_iso
from guardrail.policy.caps import CapsEvaluationError, evaluate_result_caps
from guardrail.policy.expr import ExpressionError, evaluate
from guardrail.policy.single import SingleCallPolicy
from guardrail.protocols import SessionRecord
from guardrail.provenance import ProvenanceStore, check_requirements
from guardrail.tools.contracts import ArgsValidationError, validate_args
from guardrail.tools.registry import TOOL_SPECS

# 判定链顺序（spec §6.1）。写成模块级常量是为了让顺序本身可被测试与文档引用——
# 「顺序不可调换」这句话如果没有一个可断言的对象，就只是一句注释。
#
# 与 spec §6.1 编号的两处有意偏离：
#   1. provenance（第 2 步）与工具白名单（第 3 步）互换。provenance 要读工具
#      自己声明的 requires，白名单都还没过就没有 requires 可读。
#   2. 幂等（spec §6.1 第 8 步）不在这里——它必须横跨「审计 → 执行 → 记账」，
#      塞不进一条纯前置的判定链。它由 api/tools.py 在执行前后两步完成。
STEP_ORDER: tuple[str, ...] = (
    "session",
    "tool_known",
    "provenance",
    "args_schema",
    "permission",
    "single_threshold",
    "resulting_state_cap",
)


class SingleVerdict(BaseModel):
    """单次策略的判定结果。

    `kind` 区分「参数畸形」与「策略违规」：前者是调用方的错（HTTP 400），
    后者是请求本身不被允许（HTTP 403）。Agent 靠这个区分该改参数还是该换做法。
    结果层规则由 policy/caps.py 在本模块之后接上，两者是同一个 verdict 对象。
    """

    decision: Literal["allow", "deny"]
    kind: Literal["policy", "invalid_args"] = "policy"
    rule_id: str | None = None
    reasons: list[str] = Field(default_factory=list)


def _deny(reason: str, *, kind: str = "policy", rule_id: str | None = None) -> SingleVerdict:
    return SingleVerdict(decision="deny", kind=kind, rule_id=rule_id, reasons=[reason])


def is_session_valid(record: SessionRecord, now: str) -> bool:
    """会话是否存在且未过期。

    时间戳解析失败按过期处理：多拒一次的代价，远小于让一个坏时间戳换来一个
    永久有效的会话。缺时区同样按无效处理——`sweep_expired` 依赖字符串比较，
    无时区的时间戳与它不在同一个可比空间里。
    """
    try:
        expires_at = datetime.fromisoformat(record.expires_at)
        current = datetime.fromisoformat(now)
    except ValueError:
        return False
    if expires_at.tzinfo is None or current.tzinfo is None:
        return False
    return expires_at > current


async def evaluate_single_call(
    *,
    record: SessionRecord,
    tool: str,
    args: dict[str, Any],
    policy: SingleCallPolicy,
    provenance_store: ProvenanceStore,
    shop: httpx.AsyncClient,
    check_provenance: bool = True,
    check_caps: bool = True,
) -> SingleVerdict:
    """check_provenance / check_caps 供计划提交链（plans.submit_plan）关闭：
    - provenance：计划内步骤的依赖（券→下单）由投影与计划结构保证，真实实体
      引用留到执行时校验；
    - caps：单步结果态上限被计划级投影覆盖（终态语义更强）。

    其余行为同原语义：按 STEP_ORDER 求值一次工具调用。
    """
    # 1 会话有效性
    if not is_session_valid(record, now_iso()):
        return _deny(f"会话已过期或不可用：{record.session_id}")

    # 2 工具白名单
    spec = TOOL_SPECS.get(tool)
    if spec is None:
        return _deny(f"未知工具：{tool}")

    # 3 Provenance
    if check_provenance:
        reason = await check_requirements(provenance_store, record.session_id, spec, args)
        if reason is not None:
            return _deny(reason)

    # 4 参数契约
    try:
        validate_args(spec, args)
    except ArgsValidationError as exc:
        return _deny(str(exc), kind="invalid_args")

    # 5 权限
    if not policy.is_permitted(record.agent_id, tool):
        return _deny(f"agent {record.agent_id!r} 无权调用工具 {tool!r}")

    # 6 单次阈值（语法层）
    for rule in policy.rules_for(tool):
        if rule.deny_if is None:
            continue
        try:
            hit = bool(evaluate(rule.deny_if, args))
        except ExpressionError as exc:
            # 表达式求值失败按拒绝处理，而不是按「没命中」放行（spec §10.2）。
            return _deny(
                f"规则 {rule.id!r} 求值失败，已按 fail-closed 拒绝本次调用：{exc}",
                rule_id=rule.id,
            )
        if hit:
            return _deny(rule.message, rule_id=rule.id)

    # 7 结果态上限（spec §6.3）。没有结果层规则时这一步不做任何投影。
    try:
        cap_hit = (
            await evaluate_result_caps(tool=tool, args=args, policy=policy, shop=shop)
            if check_caps
            else None
        )
    except CapsEvaluationError as exc:
        return _deny(str(exc))
    if cap_hit is not None:
        rule_id, message = cap_hit
        return _deny(message, rule_id=rule_id)

    return SingleVerdict(decision="allow")
