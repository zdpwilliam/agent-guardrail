"""计划 API（spec §4.2 / §4.4）。

审批对象分离：单次扣下走 pending_approvals（M3），整份计划走 plans（本模块）。
控制台（M5）分两个队列展示——两者的生命周期和审批人动作完全不同。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from guardrail.api.auth import require_agent, require_approver
from guardrail.audit import AuditDraft
from guardrail.clock import now_iso
from guardrail.plans import PlanDecision, PlannedAction, plan_token_for
from guardrail.protocols import DecisionEvent

router = APIRouter(prefix="/v1", tags=["plans"])


class PlanSubmission(BaseModel):
    session_id: str
    intent: str = ""
    actions: list[PlannedAction] = Field(default_factory=list)


class ResolutionBody(BaseModel):
    resolution: Literal["approve", "reject"]
    decided_by: str
    comment: str | None = None


@router.post("/plans")
async def submit_plan_endpoint(
    request: Request,
    body: PlanSubmission,
    authenticated_agent: str | None = Depends(require_agent),
) -> PlanDecision:
    """提交计划：预检 → 投影 → 分级。low 自动签发 token，medium/high 等审批。"""
    state = request.app.state
    record = await state.sessions.load(body.session_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {body.session_id}")
    if authenticated_agent is not None and record.agent_id != authenticated_agent:
        raise HTTPException(status_code=403, detail="认证身份不属于当前会话")
    session_state, _ = await state.sessions.load_state(body.session_id)
    if session_state is None:
        raise HTTPException(status_code=404, detail=f"会话状态不存在: {body.session_id}")

    plan, decision = await state.submit_plan(
        plan_store=state.plans,
        session_store=state.sessions,
        provenance_store=state.provenance,
        single_policy=state.policy,
        combined=state.combined_policy,
        plan_policy=state.plan_policy,
        record=record,
        state=session_state,
        intent=body.intent,
        actions=body.actions,
        shop=state.shop,
    )
    if decision.risk_level == "rejected":
        raise HTTPException(status_code=422, detail="; ".join(decision.reasons))
    return decision


@router.get("/plans")
async def list_plans(
    request: Request,
    status: str = "pending",
    authenticated_agent: str | None = Depends(require_agent),
) -> list:
    plans = await request.app.state.plans.list_by_status(status)
    if authenticated_agent is not None:
        plans = [p for p in plans if p.agent_id == authenticated_agent]
    return [p.model_dump(mode="json") for p in plans]


@router.get("/plans/{plan_id}")
async def get_plan(
    request: Request,
    plan_id: str,
    authenticated_agent: str | None = Depends(require_agent),
) -> dict:
    plan = await request.app.state.plans.load(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"计划不存在: {plan_id}")
    if authenticated_agent is not None and plan.agent_id != authenticated_agent:
        raise HTTPException(status_code=403, detail="认证身份无权访问该计划")
    return plan.model_dump(mode="json")


@router.post("/plans/{plan_id}/resolve")
async def resolve_plan(
    request: Request,
    plan_id: str,
    body: ResolutionBody,
    authenticated_approver: str | None = Depends(require_approver),
) -> dict:
    decided_by = authenticated_approver or body.decided_by
    return await resolve_plan_once(
        request.app.state, plan_id, body.resolution, decided_by, body.comment
    )


async def resolve_plan_once(
    state: Any,  # noqa: ANN401 - FastAPI app.state
    plan_id: str,
    resolution: str,
    decided_by: str,
    comment: str | None = None,
) -> dict:
    """审批一次计划。API 与控制台（M5）共用——审批语义必须只有一份。"""
    plan = await state.plans.load(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"计划不存在: {plan_id}")
    if plan.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=(f"计划 {plan_id} 已是 {plan.status}，不可审批——"
                    "批准的是这一个计划，不是任意授权"),
        )

    if resolution == "reject":
        await state.plans.update_status(plan_id, "rejected", decided_by=decided_by)
        await _audit(state, plan, "deny",
                     [f"审批人 {decided_by} 拒绝计划"] + _comment(comment))
        state.bus.publish(DecisionEvent(
            session_id=plan.session_id, tool=f"plan:{plan.plan_id}", decision="deny",
            reasons=[f"审批人 {decided_by} 拒绝计划"], timestamp=now_iso(),
        ))
        return {"plan_id": plan_id, "status": "rejected",
                "decided_by": decided_by}

    # approve：签发 plan_token（TTL 从批准时刻起算）。
    token = plan_token_for(plan, state.plan_policy.token_ttl_minutes)
    await state.plans.update_status(
        plan_id, "approved", decided_by=decided_by,
        token_json=token.model_dump_json(), token_expires_at=token.exp,
    )
    await _audit(
        state, plan, "allow",
        [f"审批人 {decided_by} 批准计划"
         f"（TTL {state.plan_policy.token_ttl_minutes} 分钟）"] + _comment(comment),
    )
    state.bus.publish(DecisionEvent(
        session_id=plan.session_id, tool=f"plan:{plan.plan_id}", decision="allow",
        reasons=[f"审批人 {decided_by} 批准计划"], timestamp=now_iso(),
    ))
    return {"plan_id": plan_id, "status": "approved",
            "decided_by": decided_by, "token": token.model_dump(mode="json")}


def _comment(comment: str | None) -> list[str]:
    return [f"备注：{comment}"] if comment else []


async def _audit(
    state: Any,  # noqa: ANN401 - FastAPI app.state
    plan: Any,  # noqa: ANN401 - Plan 模型
    decision: str,
    reasons: list[str],
) -> None:
    """计划级审计：tool 字段记为 plan:{id}，与单调用审计同链不同形。"""
    await state.audit.append(AuditDraft(
        session_id=plan.session_id,
        tool=f"plan:{plan.plan_id}",
        args={"intent": plan.intent, "steps": len(plan.actions)},
        decision=decision, reasons=reasons, timestamp=now_iso(),
    ))
