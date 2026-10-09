from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from guardrail.api.auth import require_agent
from guardrail.audit import AuditDraft
from guardrail.clock import now_iso
from guardrail.idempotency import idempotency_key
from guardrail.models import ProjectedState, compute_metrics
from guardrail.policy.engine import evaluate_single_call
from guardrail.preview_rules import assert_action_applicable
from guardrail.projection import ProjectionError, build_shadow, project
from guardrail.projection_args import PlannedAction, derive_projection_args
from guardrail.protocols import DecisionEvent, PendingApproval
from guardrail.provenance import register_result
from guardrail.session import VersionConflictError, authorize, commit_effect
from guardrail.shadow_loader import ShadowLoadError, load_referenced_entities
from guardrail.tools.handlers import ToolContext, handle
from guardrail.tools.registry import TOOL_SPECS

router = APIRouter(prefix="/v1", tags=["tools"])


class ToolCall(BaseModel):
    session_id: str
    args: dict[str, Any] = {}
    # M4：计划执行时携带。带 plan_id 的调用走 §4.5 三重校验分支；
    # 不带则走 M3 单调用路径，两条路径共用同一个端点与响应形状。
    plan_id: str | None = None


class PlanPreview(BaseModel):
    session_id: str
    actions: list[PlannedAction]


async def _require_session(
    request: Request,
    session_id: str,
    authenticated_agent: str | None,
) -> None:
    record = await request.app.state.sessions.load(session_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    if authenticated_agent is not None and record.agent_id != authenticated_agent:
        raise HTTPException(status_code=403, detail="认证身份不属于当前会话")


@router.post("/tools/{name}")
async def call_tool(
    request: Request,
    name: str,
    body: ToolCall,
    authenticated_agent: str | None = Depends(require_agent),
) -> dict[str, Any]:
    """一次工具调用的完整网关路径。

    顺序（spec §5 路径 1）：
        1. 加载会话（不存在 → 404）
        2. 幂等快速路径：已完成 → 原样返回首次响应
        3. 单次判定链：会话 / 白名单 / Provenance / 参数契约 / 权限 / 阈值 / 结果态上限
        4. 幂等占位登记（被占用 → 409）
        5. **写审计（先于执行）**
        6. 执行；失败则释放幂等键并把商城错误透传
        7. 登记 provenance、完成幂等、写执行结果审计

    三处与 spec 的偏离，都是实现顺序上的必然，写在这里以免后来者「修正」回去：

    - **幂等被拆成执行前后两步**（spec §6.1 把它列为判定链第 8 步）。它必须
      横跨「审计 → 执行 → 记账」，塞不进一条纯前置的判定链。快速路径（已完成）
      放在判定之前，避免为一个已知会重放的调用白跑一遍投影；占位登记放在执行
      之前，用一次条件写把并发重放挡在 409。计划 ③ 接上风险预算后，预算扣减
      必须放在占位登记之后、重放分支之前——否则一次重放会扣两次预算。
    - **放行会写两条审计**：一条「批准并即将执行」，一条「执行完成/失败」。
      spec §7 要求审计先于执行（这样不存在「执行了但没记上」的窗口），而执行
      结果只有执行后才知道。链是只追加的，所以补第二条而不是改第一条。
    - **未知工具也进审计**：被污染的模型开始试探工具名，正是最早期的信号。
    """
    state = request.app.state
    record = await state.sessions.load(body.session_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {body.session_id}")
    if authenticated_agent is not None and record.agent_id != authenticated_agent:
        raise HTTPException(status_code=403, detail="认证身份不属于当前会话")

    if name not in TOOL_SPECS:
        await _audit(state, body.session_id, name, body.args, "deny", [f"未知工具: {name}"])
        raise HTTPException(status_code=404, detail=f"未知工具: {name}")

    key = idempotency_key(body.session_id, name, body.args)
    cached = await state.idempotency.get(key)
    if cached is not None and cached.status == "done" and cached.response is not None:
        await _audit(
            state,
            body.session_id,
            name,
            body.args,
            "allow",
            ["幂等重放：返回首次响应，未重新执行"],
            replay=True,
        )
        return {**cached.response, "replayed": True}

    eval_mode = getattr(state.settings, "eval_mode", "full")
    plan = None
    args = body.args
    if eval_mode == "full" and body.plan_id is not None:
        plan, matched = await _validate_plan_step(
            state, body.plan_id, body.session_id, name, body.args)
        # 计划内占位符（preview-coupon-N）替换为前序步骤的真实产出。
        # 哈希校验仍针对封存 args（上面已完成）——替换只发生在执行语义层。
        args = _substitute_placeholders(dict(matched.args), plan.outputs)

    # eval_mode=none：无护栏对照系统，语法层也不做（spec §12.3 下界参照）。
    verdict = None
    if eval_mode != "none":
        verdict = await evaluate_single_call(
            record=record,
            tool=name,
            args=args,
            policy=state.policy,
            provenance_store=state.provenance,
            shop=state.shop,
        )
    if verdict is not None and verdict.decision == "deny":
        await _audit(state, body.session_id, name, body.args, "deny", verdict.reasons)
        status = 400 if verdict.kind == "invalid_args" else 403
        raise HTTPException(status_code=status, detail="; ".join(verdict.reasons))

    if not await state.idempotency.begin(key, body.session_id):
        await _audit(
            state,
            body.session_id,
            name,
            body.args,
            "deny",
            ["同一调用正在执行中，请稍后重试"],
        )
        raise HTTPException(status_code=409, detail="同一调用正在执行中，请稍后重试")

    # ---- 组合风险 + 预算决策（spec §3.5：单次策略 → 组合风险 → 预算 → 执行）----
    # eval_mode=single：只做单次判定（同类方案常见基线，spec §12.3 对照系统），
    # 在执行分支里直接放行——见下方 eval_mode != "full" 的执行段。

    # eval_mode != full 时跳过本段（评测对照系统，spec §12.3）。
    if eval_mode != "full":
        try:
            result = await handle(name, body.args,
                              ToolContext(shop=state.shop, corp=state.corp))
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=exc.response.status_code, detail=exc.response.text
            ) from exc
        except KeyError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"工具 {name!r} 缺少必需参数 {exc.args[0]!r}"
            ) from exc
        await register_result(state.provenance, body.session_id, TOOL_SPECS[name], result)
        return {"tool": name, "decision": "allow", "result": result,
                "reasons": [], "flagged": False, "replayed": False,
                "eval_mode": eval_mode}


    # M3 接线说明： authorize 在幂等占位**之后**——并发重放已被挡在 409；
    # 预算扣减发生在 commit_effect（生效提交段），重放分支在上面已经 return，
    # 所以「重放扣两次预算」的窗口不存在。
    try:
        auth = await authorize(
            state.sessions, state.combined_policy, body.session_id, name, body.args
        )
    except VersionConflictError as exc:
        await state.idempotency.release(key)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if auth.decision == "deny":
        # 组合 deny：调用没生效不扣预算；释放幂等键让 Agent 换方式重试。
        # detail 同时给规则 id（机器可定位）与消息（人可读）。
        await _audit(state, body.session_id, name, body.args, "deny",
                     [*auth.reasons, f"规则: {auth.rule_ids}" if auth.rule_ids else ""])
        await state.idempotency.release(key)
        detail = "; ".join(
            f"[{rid}] {msg}" for rid, msg in zip(auth.rule_ids, auth.reasons, strict=False)
        ) or "; ".join(auth.reasons) or "组合风险拒绝"
        raise HTTPException(status_code=403, detail=detail)

    if auth.decision == "ask":
        # 预算阶梯扣下：建审批单、幂等键保持 in_progress（批准→complete，
        # 拒绝→release），202 告知调用方去向。
        created = await state.approvals.create(
            PendingApproval(
                id="",
                session_id=body.session_id,
                agent_id=record.agent_id,
                tool=name,
                args=body.args,
                reasons=auth.reasons or ["预算耗尽，扣下等待审批"],
                cost=auth.cost,
                created_at=now_iso(),
            )
        )
        await _audit(state, body.session_id, name, body.args, "ask",
                     [*auth.reasons, f"扣下等待审批：{created.id}"])
        state.bus.publish(
            DecisionEvent(
                session_id=body.session_id, tool=name, decision="ask",
                reasons=auth.reasons, timestamp=now_iso(),
            )
        )
        return JSONResponse(
            status_code=202,
            content={
                "decision": "ask",
                "pending_approval_id": created.id,
                "reasons": auth.reasons or ["预算耗尽，扣下等待审批"],
            },
        )

    await _audit_or_deny(state, body.session_id, name, body.args, "allow", auth.reasons)

    try:
        result = await handle(name, args,
                              ToolContext(shop=state.shop, corp=state.corp))
    except httpx.HTTPStatusError as exc:
        await state.idempotency.release(key)
        if eval_mode == "single":
            raise HTTPException(
                status_code=exc.response.status_code, detail=exc.response.text
            ) from exc
        await _audit(
            state,
            body.session_id,
            name,
            body.args,
            "allow",
            [*(verdict.reasons or []), f"执行失败：{exc.response.status_code}"],
        )
        if plan is not None and plan.status == "approved":
            # §4.6：部分失败不回滚，标红交人处置；已执行列表不记失败步
            #（哈希未消费，该步可重试）。
            await state.plans.update_status(plan.plan_id, "partially_applied")
        raise HTTPException(
            status_code=exc.response.status_code, detail=exc.response.text
        ) from exc
    except KeyError as exc:
        # handle 直接下标读取必需参数，缺参抛 KeyError。畸形输入必须是 400，
        # 而不是让 KeyError 逃逸成 500（商城自身的 404/409 仍由 HTTPStatusError 透传）。
        await state.idempotency.release(key)
        await _audit(
            state,
            body.session_id,
            name,
            body.args,
            "allow",
            [*(verdict.reasons or []), f"执行失败：缺少必需参数 {exc.args[0]!r}"],
        )
        raise HTTPException(
            status_code=400, detail=f"工具 {name!r} 缺少必需参数 {exc.args[0]!r}"
        ) from exc

    # 生效提交：Δ/T/A/预算扣减/flag 落盘（只记生效调用）。二次 CAS 冲突时
    # 商城已写成功——照常响应，降级事实进审计与响应，不向调用方说谎。
    committed = await commit_effect(
        state.sessions, body.session_id, auth, TOOL_SPECS[name], args, auth.cost
    )
    degrade_note = None if committed else "状态同步降级：生效提交二次冲突，预算可能少记一次"

    # provenance 登记放在执行之后：它记的是「这个实体确实被本会话产出过」。
    await register_result(state.provenance, body.session_id, TOOL_SPECS[name], result)

    if eval_mode == "single":
        return {"tool": name, "decision": "allow", "result": result,
                "reasons": [], "flagged": False, "replayed": False,
                "eval_mode": "single"}

    if plan is not None:
        await _consume_plan_step(state, plan, name, matched, result)

    response = {
        "tool": name,
        "decision": auth.decision,
        "result": result,
        "reasons": [*auth.reasons, *([degrade_note] if degrade_note else [])],
        "flagged": auth.decision == "allow_with_flag",
    }
    await state.idempotency.complete(key, response)
    await _audit(
        state,
        body.session_id,
        name,
        body.args,
        "allow",
        [*auth.reasons, *([degrade_note] if degrade_note else []), "执行完成"],
    )
    state.bus.publish(
        DecisionEvent(
            session_id=body.session_id,
            tool=name,
            decision=auth.decision,
            reasons=auth.reasons,
            timestamp=now_iso(),
        )
    )
    return {**response, **({"plan_id": body.plan_id} if body.plan_id else {}),
            "replayed": False}


async def _validate_plan_step(
    state: Any,  # noqa: ANN401 - FastAPI app.state
    plan_id: str,
    session_id: str,
    tool: str,
    args: dict[str, Any],
) -> tuple[Any, dict]:  # noqa: ANN401 - (Plan, 封存的动作)
    """§4.5 三重校验的第 1、2 条：token 有效 + 动作在计划内且未消费。

    第 3 条（重新求值）由后续的 evaluate_single_call + authorize 天然覆盖——
    计划步骤走的就是同一条判定链，用执行时刻的会话状态。
    """
    from guardrail.plans import action_hash, token_expired

    plan = await state.plans.load(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"计划不存在: {plan_id}")
    if await state.plans.expire_if_due(plan_id):
        plan = await state.plans.load(plan_id)  # 重载拿过期后的状态
    if plan is not None and plan.status == "expired":
        raise HTTPException(
            status_code=403, detail="计划已过期（token TTL 到期），后续调用被拒"
        )
    if plan.status not in ("approved", "partially_applied"):
        raise HTTPException(
            status_code=403,
            detail=f"计划未获批准（当前 {plan.status}）——批准的是这一个计划，"
                   "不是任意授权",
        )
    if plan.session_id != session_id:
        raise HTTPException(status_code=403, detail="计划不属于当前会话")
    token = plan.token()
    if token is None or token_expired(token, now_iso()):
        raise HTTPException(status_code=403, detail="plan_token 无效或已过期")
    digest = action_hash(tool, args)
    if digest not in token.action_hashes:
        raise HTTPException(
            status_code=403,
            detail="动作不在计划内（哈希失配）——批准的是这一个计划，不是任意授权",
        )
    if digest in plan.executed_hashes:
        raise HTTPException(status_code=409, detail="该步骤已执行过")
    matched = next(a for a in plan.actions
                   if action_hash(a.tool, a.args) == digest)
    return plan, matched


async def _consume_plan_step(
    state: Any,  # noqa: ANN401 - FastAPI app.state
    plan: Any,  # noqa: ANN401 - Plan 模型
    tool: str,
    matched: dict,
    result: dict[str, Any],
) -> None:
    """消费动作哈希（CAS）+ 记录本步真实产出，全部完成时推进 completed。

    消费放在执行成功之后：失败步不占坑，可重试。并发同参步骤由幂等键挡下。
    产出映射（preview-{实体类型}-{步序} → 真实 id）供后续步骤执行时替换
    ——计划 args 提交时封存，依赖前序产出的部分由网关在执行语义层解析。
    """
    from guardrail.plans import action_hash

    digest = action_hash(tool, matched.args)
    idx = matched.step
    outputs: dict[str, str] = {}
    for emit in TOOL_SPECS[tool].emits:
        value = _dig(result, emit.path)
        if value:
            outputs[f"preview-{emit.entity_type}-{idx}"] = str(value)
    for _ in range(2):
        loaded = await state.plans.load_with_version(plan.plan_id)
        if loaded is None:
            return
        _current, version = loaded
        if await state.plans.consume_hash(plan.plan_id, digest, version, outputs):
            final = await state.plans.load(plan.plan_id)
            token = final.token() if final else None
            if final is not None and token is not None:
                done = len(final.executed_hashes) >= len(token.action_hashes)
                if done:
                    await state.plans.update_status(plan.plan_id, "completed")
            return


def _dig(obj: dict[str, Any], dotted: str) -> Any:  # noqa: ANN401 - 动态路径取值
    cur: Any = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _substitute_placeholders(args: dict[str, Any], outputs: dict[str, str]) -> dict[str, Any]:
    """把封存 args 里的计划内占位符（preview-coupon-N）替换为真实 id。

    只做整串等值替换：占位符永远独占一个参数值（与 M1 投影的命名约定一致）。
    未解析的占位符原样保留——下游 provenance 门会拒绝它，宁可误伤也不放行
    一个网关无法溯源的实体引用。
    """
    return {k: outputs.get(v, v) if isinstance(v, str) else v
            for k, v in args.items()}


async def _audit(
    state: Any,  # noqa: ANN401 - FastAPI 的 app.state 没有稳定类型
    session_id: str,
    tool: str,
    args: dict[str, Any],
    decision: str,
    reasons: list[str],
    *,
    replay: bool = False,
) -> None:
    await state.audit.append(
        AuditDraft(
            session_id=session_id,
            tool=tool,
            args=args,
            decision=decision,  # type: ignore[arg-type]
            replay=replay,
            reasons=reasons,
            timestamp=now_iso(),
        )
    )


async def _audit_or_deny(
    state: Any,  # noqa: ANN401 - FastAPI 的 app.state 没有稳定类型
    session_id: str,
    tool: str,
    args: dict[str, Any],
    decision: str,
    reasons: list[str],
) -> None:
    """审计必须先于执行；写不进去就取消执行（spec §7 / §10.2）。"""
    try:
        await _audit(state, session_id, tool, args, decision, reasons)
    except Exception as exc:
        raise HTTPException(
            status_code=403, detail=f"审计链写入失败，已取消执行：{exc}"
        ) from exc


@router.post("/plans/preview")
async def preview_plan(
    request: Request,
    body: PlanPreview,
    authenticated_agent: str | None = Depends(require_agent),
) -> ProjectedState:
    """在影子状态上投影整个计划，返回终态与业务指标。不触碰真实商城。

    逐步投影，每步的派生参数以**当前影子状态**为基准——同一商品在计划里被
    改价多次时，第二次的基准必须是第一次投影后的价格，否则投影与真实执行的
    终态会对不上（Task 10 的属性测试专门验证这一点）。
    """
    await _require_session(request, body.session_id, authenticated_agent)
    shop = request.app.state.shop

    products = (await shop.get("/shop/v1/products")).json()
    try:
        coupons, orders = await load_referenced_entities(shop, body.actions)
    except ShadowLoadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    before = build_shadow(products=products, coupons=coupons, orders=orders)

    shadow = before.clone()
    for step, action in enumerate(body.actions):
        if action.tool not in TOOL_SPECS:
            raise HTTPException(status_code=400, detail=f"未知工具: {action.tool}")
        try:
            args = derive_projection_args(action, shadow, step)
            assert_action_applicable(action.tool, args, shadow)
            shadow = project(shadow, TOOL_SPECS, [(action.tool, args)])
        except ProjectionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    entities = {k: v for k, v in shadow.products.items() if f"product:{k}" in shadow.touched}
    return ProjectedState(
        entities=entities,
        metrics=compute_metrics(before, shadow),
        triggered_rules=[],
    )
