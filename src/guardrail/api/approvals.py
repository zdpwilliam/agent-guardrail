"""审批 API（M3 的最小落地；控制台 UI 在 M5，计划级审批在 M4）。

approve 的语义边界（写在这里防止 M4「顺手加固」时破坏演示语义）：
- 批准后**直接执行**，不再重跑判定链——这次调用在 authorize 时已经通过了
  全部判定（单次策略 + 组合风险 + 结果态上限），扣下纯粹因为预算阶梯。
  「批准后重校验」属于 plan_token 机制（spec §4.5，M4），与单次调用的
  ask 是两回事。
- 成本用审批单上存档的值：authorize 时的策略快照决定这次调用值多少钱，
  策略文件中途改动不追溯。
"""

from __future__ import annotations

from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from guardrail.api.auth import require_approver
from guardrail.audit import AuditDraft
from guardrail.clock import now_iso
from guardrail.idempotency import idempotency_key
from guardrail.policy.engine import is_session_valid
from guardrail.protocols import DecisionEvent
from guardrail.provenance import register_result
from guardrail.session import Authorization, commit_effect
from guardrail.tools.handlers import ToolContext, handle
from guardrail.tools.registry import TOOL_SPECS

router = APIRouter(prefix="/v1", tags=["approvals"])


class ResolutionBody(BaseModel):
    resolution: Literal["approve", "reject"]
    decided_by: str
    comment: str | None = None


@router.get("/approvals")
async def list_open(
    request: Request,
    authenticated_approver: str | None = Depends(require_approver),
) -> list:
    """开放中的待审批单。M5 控制台的审批队列直接消费这个接口。"""
    return await request.app.state.approvals.list_open()


@router.post("/approvals/{approval_id}/resolve")
async def resolve_approval(
    request: Request,
    approval_id: str,
    body: ResolutionBody,
    authenticated_approver: str | None = Depends(require_approver),
) -> dict:
    state = request.app.state
    body = body.model_copy(
        update={"decided_by": authenticated_approver or body.decided_by}
    )
    pa = await state.approvals.resolve(
        approval_id, body.resolution, body.decided_by, body.comment
    )
    if pa is None:
        existing = await state.approvals.load(approval_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"待审批单不存在: {approval_id}")
        raise HTTPException(
            status_code=409,
            detail=f"待审批单 {approval_id} 已被处理（{existing.resolution}）",
        )

    key = idempotency_key(pa.session_id, pa.tool, pa.args)

    if body.resolution == "reject":
        # 拒绝：释放幂等键，同一调用可以真正重试；预算不动（只记生效）。
        await state.idempotency.release(key)
        await _audit(state, pa, "deny", [f"审批人 {body.decided_by} 拒绝"] + _comment(body))
        state.bus.publish(DecisionEvent(
            session_id=pa.session_id, tool=pa.tool, decision="deny",
            reasons=[f"审批人 {body.decided_by} 拒绝"], timestamp=now_iso(),
        ))
        return {"decision": "rejected", "approval_id": pa.id,
                "decided_by": body.decided_by}

    return await _execute_approved(state, pa, body, key)


def _comment(body: ResolutionBody) -> list[str]:
    return [f"备注：{body.comment}"] if body.comment else []


async def _audit(
    state: Any,  # noqa: ANN401 - FastAPI app.state 是动态容器
    pa: Any,  # noqa: ANN401 - 审批单来自 app.state
    decision: str,
    reasons: list[str],
) -> None:
    await state.audit.append(AuditDraft(
        session_id=pa.session_id, tool=pa.tool, args=pa.args,
        decision=decision, reasons=reasons, timestamp=now_iso(),
    ))


async def _execute_approved(
    state: Any,  # noqa: ANN401 - FastAPI app.state 是动态容器
    pa: Any,  # noqa: ANN401 - 审批单来自 app.state
    body: ResolutionBody,
    key: str,
) -> dict:
    auth = Authorization(decision="allow", cost=pa.cost)
    audit_reasons = [f"审批人 {body.decided_by} 批准后执行"] + _comment(body)

    record = await state.sessions.load(pa.session_id)
    if record is None or not is_session_valid(record, now_iso()):
        reason = "会话已过期或不存在，审批执行已取消"
        await state.idempotency.release(key)
        await _audit(state, pa, "deny", [*audit_reasons, reason])
        state.bus.publish(DecisionEvent(
            session_id=pa.session_id, tool=pa.tool, decision="deny",
            reasons=[reason], timestamp=now_iso(),
        ))
        raise HTTPException(status_code=403, detail=reason)

    # 幂等键应由 ASK 流程在扣下时登记；这里兜底补登记，让「不经 ASK 直接
    # 构造审批单」（测试、运维补单）也能走完 approve→complete。
    await state.idempotency.begin(key, pa.session_id)

    try:
        result = await handle(
            pa.tool, pa.args, ToolContext(shop=state.shop, corp=state.corp)
        )
    except httpx.HTTPStatusError as exc:
        await state.idempotency.release(key)
        await _audit(state, pa, "allow", [*audit_reasons,
                                          f"执行失败：{exc.response.status_code}"])
        raise HTTPException(
            status_code=exc.response.status_code, detail=exc.response.text
        ) from exc
    except KeyError as exc:
        await state.idempotency.release(key)
        await _audit(state, pa, "allow", [*audit_reasons,
                                          f"执行失败：缺少必需参数 {exc.args[0]!r}"])
        raise HTTPException(
            status_code=400, detail=f"工具 {pa.tool!r} 缺少必需参数 {exc.args[0]!r}"
        ) from exc
    except Exception as exc:
        await state.idempotency.release(key)
        await _audit(
            state, pa, "allow",
            [*audit_reasons, f"执行失败：{type(exc).__name__}: {exc}"],
        )
        raise

    committed = await commit_effect(
        state.sessions, pa.session_id, auth, TOOL_SPECS[pa.tool], pa.args, pa.cost
    )
    if not committed:
        # 二次 CAS 冲突：商城已写成功，照常响应；状态新鲜度降级记入审计
        # 与 limitations（少扣预算的方向是更宽松，不是更严）。
        audit_reasons.append("状态同步降级：生效提交二次冲突，预算可能少记一次")

    await register_result(state.provenance, pa.session_id, TOOL_SPECS[pa.tool], result)

    response = {
        "tool": pa.tool, "decision": "allow", "result": result,
        "reasons": audit_reasons, "replayed": False,
    }
    await state.idempotency.complete(key, response)
    await _audit(state, pa, "allow", [*audit_reasons, "执行完成"])
    state.bus.publish(DecisionEvent(
        session_id=pa.session_id, tool=pa.tool, decision="allow",
        reasons=audit_reasons, timestamp=now_iso(),
    ))
    return {**response, "approval_id": pa.id, "decided_by": body.decided_by}
