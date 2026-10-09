"""计划级审批的模型与分级策略（spec §4）。

提交链（§4.4）：逐动作预检 → 影子投影 → 计划级组合风险 → 四级判定。
执行侧（§4.5）：plan_token 三重校验——token 有效 / 哈希未消费 / 重新求值。

`action_hash` 用整体 canonical_json 而非 spec 原式的 `tool ‖ json` 拼接：
分隔符歧义会碰撞，与 M2 幂等键同一修正（spec 回写见 M4-T5）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from guardrail.audit import canonical_json, sha256_hex
from guardrail.clock import now_iso
from guardrail.models import SessionState, compute_metrics
from guardrail.tools.registry import TOOL_SPECS

PLAN_STATUSES = (
    "pending", "approved", "rejected", "completed", "partially_applied", "expired",
)

# 合法状态迁移（spec §4.6 生命周期图）。
_LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"approved", "rejected"},
    "approved": {"completed", "partially_applied", "expired"},
    "partially_applied": {"completed", "expired"},
    "completed": set(),
    "rejected": set(),
    "expired": set(),
}


class PlannedAction(BaseModel):
    step: int
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    plan_id: str
    session_id: str
    agent_id: str
    # Agent 自述意图，仅供人参考，绝不参与判定——可能被注入污染（spec §4.2）。
    intent: str = ""
    actions: list[PlannedAction] = Field(default_factory=list)
    status: Literal[
        "pending", "approved", "rejected",
        "completed", "partially_applied", "expired",
    ] = "pending"
    risk_level: Literal["low", "medium", "high", "rejected"] | None = None
    triggered_rules: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    projected_json: str | None = None      # ProjectedState 序列化，控制台用
    token_json: str | None = None          # PlanToken 序列化，approved 后存在
    token_expires_at: str | None = None
    executed_hashes: list[str] = Field(default_factory=list)
    # 计划内占位符 → 真实实体 id（如 preview-coupon-0 → c-xxx）。
    # 计划 args 在提交时哈希封存，依赖前序产出的占位符由网关在执行时替换。
    outputs: dict[str, str] = Field(default_factory=dict)
    created_at: str = ""
    decided_at: str | None = None
    decided_by: str | None = None

    def token(self) -> PlanToken | None:
        if not self.token_json:
            return None
        return PlanToken.model_validate_json(self.token_json)


class PlanPolicy(BaseModel):
    """分级系数（policies/plan_policy.yaml，spec §18.3 全部可配置）。"""

    token_ttl_minutes: float = 15
    low_budget_floor: float = 0.30
    high_warn_count: int = 2


class PlanToken(BaseModel):
    plan_id: str
    session_id: str
    # sha256(canonical_json({tool, args}))，按计划顺序。
    action_hashes: list[str]
    exp: str  # ISO 8601 带时区


class PlanDecision(BaseModel):
    risk_level: Literal["low", "medium", "high", "rejected"]
    triggered_rules: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    plan_id: str | None = None
    token: PlanToken | None = None  # 仅 low（自动批准）时携带


def action_hash(tool: str, args: dict[str, Any]) -> str:
    """动作哈希 = sha256(canonical_json({tool, args}))。

    与 M2 幂等键同一修正立场：spec §4.5 原式 `tool ‖ canonical_json(args)`
    有分隔符歧义（tool 恰好以 json 片段结尾时两种组合拼出同一串）。
    token 防篡改的正确性建立在这个哈希上，歧义即漏洞。
    """
    return sha256_hex(canonical_json({"tool": tool, "args": args}))


def plan_token_for(plan: Plan, ttl_minutes: float) -> PlanToken:
    hashes = [action_hash(a.tool, a.args) for a in plan.actions]
    exp = (datetime.now(UTC) + timedelta(minutes=ttl_minutes)).isoformat()
    return PlanToken(plan_id=plan.plan_id, session_id=plan.session_id,
                     action_hashes=hashes, exp=exp)


def is_legal_transition(current: str, target: str) -> bool:
    return target in _LEGAL_TRANSITIONS.get(current, set())


def timestamp_expired(ts: str, now: str) -> bool:
    """过期判定。解析失败按已过期（fail-closed，与 M2 会话校验同一立场）。"""
    try:
        exp = datetime.fromisoformat(ts)
        current = datetime.fromisoformat(now)
    except ValueError:
        return True
    if exp.tzinfo is None or current.tzinfo is None:
        return True
    return exp <= current


def token_expired(token: PlanToken, now: str) -> bool:
    return timestamp_expired(token.exp, now)


# ---------- 提交链（spec §4.2-§4.4） ----------


class PlanPrecheckError(Exception):
    """计划被语法层预检拒绝（rejected，不进审批）。"""


async def submit_plan(
    *,
    plan_store: Any,  # noqa: ANN401 - PlanStore 协议实现，跨层注入
    session_store: Any,  # noqa: ANN401
    provenance_store: Any,  # noqa: ANN401
    single_policy: Any,  # noqa: ANN401 - SingleCallPolicy
    combined: Any,  # noqa: ANN401 - CombinedPolicy
    plan_policy: Any,  # noqa: ANN401 - 分级系数容器
    record: Any,  # noqa: ANN401 - SessionRecord
    state: SessionState,
    intent: str,
    actions: list[PlannedAction],
    shop: Any,  # noqa: ANN401 - httpx.AsyncClient
) -> tuple[Plan, PlanDecision]:
    """提交计划：逐动作预检 → 影子投影 → 计划级组合风险 → 四级判定。

    计划级组合风险的终态语义：把计划各步增量叠到**会话状态副本**上（不改库），
    对四类规则求值——「计划执行完之后是否越界」只有投影能发现（§4.4③）。
    途中逐步告警会造成 N 次审批，正是 §4.1 要消灭的东西。
    """
    import uuid

    from guardrail.policy.caps import evaluate_result_caps_for_projection
    from guardrail.policy.combined import (
        apply_risk_deltas,
        evaluate_cross_agent,
        evaluate_cumulative,
        evaluate_sequence,
        evaluate_taint,
    )
    from guardrail.policy.engine import evaluate_single_call
    from guardrail.preview_rules import assert_action_applicable
    from guardrail.projection import project
    from guardrail.projection_args import PlannedAction as PaAction
    from guardrail.projection_args import derive_projection_args
    from guardrail.shadow_loader import ShadowLoadError, load_shadow

    plan_id = f"pl-{uuid.uuid4().hex[:12]}"
    projected_json: str | None = None

    async def _save_and_decide(
        status: str,
        risk_level: str,
        triggered: list[str],
        reasons: list[str],
        token: PlanToken | None = None,
    ) -> tuple[Plan, PlanDecision]:
        plan = Plan(
            plan_id=plan_id, session_id=record.session_id, agent_id=record.agent_id,
            intent=intent, actions=actions, status=status, risk_level=risk_level,
            triggered_rules=triggered, reasons=reasons, created_at=now_iso(),
            projected_json=projected_json,
        )
        if token is not None:
            plan.token_json = token.model_dump_json()
            plan.token_expires_at = token.exp
        await plan_store.create(plan)
        decision = PlanDecision(
            risk_level=risk_level, triggered_rules=triggered, reasons=reasons,
            plan_id=plan_id, token=token,
        )
        return plan, decision

    async def _reject(reason: str) -> tuple[Plan, PlanDecision]:
        return await _save_and_decide("rejected", "rejected", [], [reason])

    if not actions:
        return await _reject("计划没有任何动作")

    # ① 语法层预检：会话 / 白名单 / schema / 权限 / 单次阈值。
    # provenance 与单步 caps 关闭——理由见 evaluate_single_call 的 docstring。
    for a in actions:
        verdict = await evaluate_single_call(
            record=record, tool=a.tool, args=a.args, policy=single_policy,
            provenance_store=provenance_store, shop=shop,
            check_provenance=False, check_caps=False,
        )
        if verdict.decision == "deny":
            prefix = f"[{verdict.rule_id}] " if verdict.rule_id else ""
            return await _reject(
                f"第 {a.step} 步预检失败：{prefix}{'; '.join(verdict.reasons)}"
            )

    # ② 影子投影 + 会话状态副本（不改库）。
    try:
        shadow_before = await load_shadow(
            shop, [(a.tool, a.args) for a in actions]
        )
    except ShadowLoadError as exc:
        return await _reject(f"计划引用的实体不存在：{exc}")

    shadow = shadow_before.clone()
    state2 = state.model_copy(deep=True)
    plan_entities: dict = {}      # 仅计划自身贡献的 Δ（跨 Agent 规则的 pending）
    plan_touched: set[str] = set()
    derived_by_step: list[tuple[str, dict]] = []
    taint_acc = set(state.taint)

    try:
        for i, a in enumerate(actions):
            spec = TOOL_SPECS[a.tool]
            derived = derive_projection_args(
                PaAction(tool=a.tool, args=a.args), shadow, i
            )
            assert_action_applicable(a.tool, derived, shadow)
            before_step = shadow.clone()
            shadow = project(shadow, TOOL_SPECS, [(a.tool, derived)])
            cap_hit = evaluate_result_caps_for_projection(
                a.tool, single_policy, before_step, shadow
            )
            if cap_hit is not None:
                rule_id, message = cap_hit
                return await _reject(
                    f"第 {a.step} 步预检失败：[{rule_id}] {message}"
                )
            derived_by_step.append((a.tool, derived))

            pending, touched = apply_risk_deltas(spec, derived, state2.entities)
            for k in touched:
                state2.entities[k] = pending[k]
            plan_touched |= touched
            plan_pending, _ = apply_risk_deltas(spec, derived, plan_entities)
            for k, v in plan_pending.items():
                plan_entities[k] = v
            taint_acc |= set(spec.taint_categories)
    except Exception as exc:  # ProjectionError / ExpressionError → fail-closed
        return await _reject(f"计划投影失败：{exc}")

    projected_json = json.dumps({
        # 计划终态：受影响实体的会话 Δ + 业务指标（§4.3 的那组派生数字）。
        "delta": {k: v.model_dump() for k, v in state2.entities.items()
                  if k in plan_touched},
        "metrics": compute_metrics(shadow_before, shadow).model_dump(),
    }, ensure_ascii=False)

    # ③ 计划级组合风险（终态语义）。
    hits = []
    hits += evaluate_cumulative(combined.rules_of("cumulative"), state2.entities,
                                plan_touched)
    if len(actions) == 1:
        hits += evaluate_sequence(combined.rules_of("sequence"),
                                  list(state2.actions),
                                  actions[0].tool, derived_by_step[0][1])
    else:
        hits += evaluate_sequence(combined.rules_of("sequence"),
                                  [*state2.actions, *actions[:-1]],
                                  actions[-1].tool, derived_by_step[-1][1])
    for _a, (tool_i, args_i) in zip(actions, derived_by_step, strict=True):
        hits += evaluate_taint(combined.rules_of("taint"), taint_acc, tool_i, args_i)
    siblings = (
        await session_store.find_by_task(record.task_id)
        if record.task_id is not None else []
    )
    hits += evaluate_cross_agent(combined.rules_of("cross_agent"),
                                 record.agent_id, record.task_id,
                                 plan_entities, siblings)

    deny = any(h.severity == "deny" for h in hits)
    warn_count = sum(1 for h in hits if h.severity == "warn")
    rule_ids = [h.rule_id for h in hits]
    messages = [h.message for h in hits]

    # ④ 分级（spec §4.4）。
    if deny or warn_count >= plan_policy.high_warn_count:
        return await _save_and_decide("pending", "high", rule_ids, messages)
    if warn_count >= 1:
        return await _save_and_decide("pending", "medium", rule_ids, messages)
    if state.risk_budget > plan_policy.low_budget_floor:
        token = plan_token_for(
            Plan(plan_id=plan_id, session_id=record.session_id,
                 agent_id=record.agent_id, actions=actions),
            plan_policy.token_ttl_minutes,
        )
        return await _save_and_decide("approved", "low", [], ["无规则触发，自动批准"],
                                      token=token)
    return await _save_and_decide(
        "pending", "medium", [],
        [f"会话预算 {state.risk_budget:.2f} 低于自动批准线，需人工确认"],
    )
