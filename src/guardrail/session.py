"""会话编排：授权段（版本双检）与生效提交段（CAS）。

两段式结构来自 spec §3.7 与本计划拍板的「只记生效调用」语义：

- **授权段** `authorize`：加载快照 → 组合风险求值 → 预算决策。**不写状态**——
  deny 不扣（没生效不消耗）、ask 不扣（批准后执行时才扣）、allow 的扣减也
  推迟到提交段。所以「求值与扣减原子」在这里落成「决策所依据的快照仍然新鲜」
  的版本双检：提交前重读版本，变了就重载重算；二次失配抛 VersionConflict
  （API 层映射 409，spec §3.7.3「绝不尽力而为地放行」指的就是这里）。
- **提交段** `commit_effect`：执行成功后把本调用增量（Δ/T/A/扣减/flag）CAS
  落盘。冲突时**重载最新状态、重放本调用增量、再存**——增量是 args 的纯函数，
  重放不会重复计入。二次失配返回 False：此时商城已写成功，向调用方返回失败
  是说谎，只能照常响应并在审计里记录「状态同步降级」。少记预算的方向是更
  宽松（漏判风险），不是更严（误伤风险），且单进程 demo 下概率极低——
  该取舍写入 limitations。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from guardrail.clock import now_iso
from guardrail.models import ActionRecord, SessionState
from guardrail.policy.budget import classify_cost, synth_decision
from guardrail.policy.combined import (
    CombinedVerdict,
    ExpressionError,
    apply_risk_deltas,
    evaluate_combined,
)
from guardrail.tools.registry import TOOL_SPECS

if TYPE_CHECKING:  # pragma: no cover
    from guardrail.policy.combined import CombinedPolicy


class VersionConflictError(Exception):
    """授权段版本双检二次失配（spec §3.7.3）。API 层必须映射 409。"""


class Authorization(BaseModel):
    """授权段的产出。deny/ask 携带判定信息；allow/flag 另带提交段需要的成本。"""

    decision: Literal["allow", "allow_with_flag", "ask", "deny"]
    version: int = 0
    cost: float = 0.0
    rule_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


def _deny(reason: str) -> Authorization:
    return Authorization(decision="deny", reasons=[reason])


async def authorize(
    store: object,
    combined: CombinedPolicy,
    session_id: str,
    tool: str,
    args: dict,
) -> Authorization:
    """授权段：组合风险求值 + 预算决策。不写任何状态。"""
    spec = TOOL_SPECS[tool]
    for _attempt in range(2):
        loaded = await store.load_state(session_id)  # type: ignore[attr-defined]
        if loaded is None:
            # 身份行刚被过期清理扫掉（API 层查过它还存在）——fail-closed。
            return _deny("会话状态不存在（可能已被过期清理）")
        state, version = loaded

        record = await store.load(session_id)  # type: ignore[attr-defined]
        if record is None:
            return _deny("会话不存在")

        siblings = None
        if record.task_id is not None:
            siblings = await store.find_by_task(record.task_id)  # type: ignore[attr-defined]

        try:
            verdict, _pending, _touched = evaluate_combined(
                combined, state, record.agent_id, record.task_id, spec, tool, args,
                siblings,
            )
        except ExpressionError as exc:
            return _deny(f"组合风险求值失败，已按 fail-closed 拒绝：{exc}")

        if verdict.deny:
            return Authorization(decision="deny", version=version,
                                 rule_ids=verdict.rule_ids, reasons=verdict.reasons)

        base_cost = classify_cost(combined, tool)
        decision, cost = synth_decision(
            state.risk_budget, base_cost, verdict.warn_count, combined.budget
        )
        if decision == "deny":
            # 预算段 deny（透支）：没有组合规则命中，原因要自己说清楚——
            # 否则 Agent 只看到一句「组合风险拒绝」，无从判断该停还是该换方式。
            return Authorization(
                decision="deny",
                version=version,
                rule_ids=verdict.rule_ids,
                reasons=[
                    f"预算透支：剩余 {state.risk_budget:.2f}，低于下限 "
                    f"{combined.budget.thresholds.deny_below}，会话已锁定"
                ],
            )
        if decision == "ask":
            return Authorization(decision="ask", version=version, cost=cost,
                                 rule_ids=verdict.rule_ids, reasons=verdict.reasons)

        # allow / allow_with_flag：提交前确认快照仍然新鲜（版本双检）。
        recheck = await store.load_state(session_id)  # type: ignore[attr-defined]
        if recheck is None:
            return _deny("会话状态不存在（可能已被过期清理）")
        fresh_state, fresh_version = recheck
        if fresh_version == version:
            return Authorization(decision=decision, version=version, cost=cost,
                                 rule_ids=verdict.rule_ids, reasons=verdict.reasons)
        # 版本变了：有人在授权期间写入。重载重算（spec §3.7.3）。
    raise VersionConflictError(
        f"会话 {session_id} 授权期间持续并发写入，二次失配（spec §3.7.3）"
    )


async def commit_effect(
    store: object,
    session_id: str,
    auth: Authorization,
    spec: object,
    args: dict,
    cost: float,
) -> bool:
    """生效提交：把本调用增量 CAS 落盘。返回 False = 二次冲突（调用方自行降级）。

    增量（Δ/T/A/扣减/flag）全部重放在**最新状态**上：Δ 声明是 args 的纯函数，
    重放不会重复计入；抢先写入的其他实体的增量因此不丢。
    """
    tool = spec.name  # type: ignore[attr-defined]
    for _attempt in range(2):
        loaded = await store.load_state(session_id)  # type: ignore[attr-defined]
        if loaded is None:
            return False
        state, version = loaded

        new_state = _apply_effect(state, spec, args, cost, tool,
                                  flag=(auth.decision == "allow_with_flag"))
        saved = await store.save_state(  # type: ignore[attr-defined]
            session_id, new_state, expected_version=version
        )
        if saved:
            return True
    return False


def _apply_effect(
    state: SessionState,
    spec: object,
    args: dict,
    cost: float,
    tool: str,
    *,
    flag: bool,
) -> SessionState:
    """在状态副本上应用一次生效调用的全部增量。"""
    new_state = state.model_copy(deep=True)
    pending, _touched = apply_risk_deltas(spec, args, new_state.entities)  # type: ignore[arg-type]
    for key in _touched:
        new_state.entities[key] = pending[key]
    new_state.taint |= set(spec.taint_categories)  # type: ignore[attr-defined]

    last_seq = new_state.actions[-1].seq if new_state.actions else 0
    new_state.actions.append(
        ActionRecord(seq=last_seq + 1, tool=tool, args=dict(args), timestamp=now_iso())
    )
    if len(new_state.actions) > 200:
        # 截断至最近 200 条（spec §3.2）。序列规则只看尾部，头部丢失无害。
        new_state.actions = new_state.actions[-200:]

    new_state.risk_budget -= cost
    new_state.flagged = new_state.flagged or flag
    return new_state


def pending_verdict_summary(verdict: CombinedVerdict) -> str:
    """把判定压缩成一行人话，审计与响应里用。"""
    if not verdict.hits:
        return "无组合风险命中"
    return "；".join(f"{h.rule_id}({h.severity})" for h in verdict.hits)
