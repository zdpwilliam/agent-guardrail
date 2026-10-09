"""控制台（spec §9）：服务端渲染，Tailwind CDN + htmx，无构建步骤。

三个组件——预算条（组合风险唯一能一眼看懂的形式）、计划卡片（把 30 步折叠成
一张卡 + 终态对比）、审计时间线。审批逻辑与 plans_api 共用 `resolve_plan_once`，
审批语义只有一份。
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from guardrail.api.plans_api import resolve_plan_once

router = APIRouter(tags=["console"])
CSRF_COOKIE = "guardrail_csrf"

# demo 模式（spec §9.3）：场景选择器一键顺序播放。结果存模块级——demo 定位，
# 非并发安全场景；生产控制台应换成任务队列。
_demo_state: dict[str, list | None] = {"results": None}
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[1] / "templates")
)


def _budget_color(budget: float) -> str:
    """预算条三段色（spec §9.2①）：绿 → 黄 → 红。"""
    if budget > 0.30:
        return "bg-emerald-500"
    if budget > 0.10:
        return "bg-amber-500"
    return "bg-red-500"


def _budget_width(budget: float) -> float:
    return max(0.0, min(100.0, budget * 100))


def _verify_csrf(request: Request, submitted: str) -> None:
    expected = request.cookies.get(CSRF_COOKIE)
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


async def require_csrf(
    request: Request,
    csrf_token: str | None = Form(default=None),
) -> None:
    _verify_csrf(request, csrf_token or "")


@router.get("/console/login", response_class=HTMLResponse)
async def console_login_page(request: Request) -> HTMLResponse:
    csrf_token = request.state.csrf_token
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="zh">
        <head><meta charset="utf-8"><title>控制台登录</title></head>
        <body>
          <h1>控制台登录</h1>
          <form method="post" action="/console/login">
            <input type="hidden" name="csrf_token" value="{csrf_token}">
            <input type="password" name="console_key" placeholder="Console key" required>
            <button type="submit">登录</button>
          </form>
        </body>
        </html>
        """
    )


@router.post("/console/login")
async def console_login(
    request: Request,
    console_key: str = Form(...),
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    expected = request.app.state.settings.console_key
    if expected is None:
        return RedirectResponse("/console", status_code=303)
    if not secrets.compare_digest(console_key, expected):
        return JSONResponse({"detail": "控制台鉴权失败"}, status_code=401)
    response = RedirectResponse("/console", status_code=303)
    response.set_cookie(
        "guardrail_console_key",
        console_key,
        httponly=True,
        secure=request.app.state.settings.environment == "production",
        samesite="strict",
        max_age=8 * 60 * 60,
    )
    return response


@router.post("/console/logout")
async def console_logout(
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    response = RedirectResponse("/console/login", status_code=303)
    response.delete_cookie("guardrail_console_key")
    response.delete_cookie(CSRF_COOKIE)
    return response


async def _session_rows(state: Any) -> list[dict]:  # noqa: ANN401 - app.state  # noqa: ANN401 - app.state
    rows = await state.backend.fetchall(
        "SELECT id, agent_id, task_id, created_at FROM sessions"
        " ORDER BY created_at DESC LIMIT 50"
    )
    out = []
    for r in rows:
        loaded = await state.sessions.load_state(r["id"])
        budget = loaded[0].risk_budget if loaded else 1.0
        flagged = loaded[0].flagged if loaded else False
        out.append({
            "id": r["id"], "agent_id": r["agent_id"], "task_id": r["task_id"],
            "created_at": r["created_at"], "budget": budget, "flagged": flagged,
            "color": _budget_color(budget), "width": _budget_width(budget),
        })
    return out


@router.get("/console", response_class=HTMLResponse)
async def console_home(request: Request, demo: str = "") -> HTMLResponse:
    state = request.app.state
    sessions = await _session_rows(state)
    pending_plans = await state.plans.list_by_status("pending")
    return templates.TemplateResponse(request, "sessions.html", {
        "sessions": sessions, "pending_plans": pending_plans,
        "demo": demo == "1", "demo_results": _demo_state["results"],
    })


@router.post("/console/demo/run")
async def console_demo_run(
    request: Request,
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    """一键顺序播放 spec §13 的 4 个场景（真实流量走同一网关）。"""
    from guardrail.demo import run_scenarios

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=request.app), base_url="http://console"
    ) as c:
        _demo_state["results"] = await run_scenarios(c)
    return RedirectResponse("/console?demo=1", status_code=303)


@router.get("/console/sessions/{session_id}", response_class=HTMLResponse)
async def console_session(request: Request, session_id: str) -> HTMLResponse:
    state = request.app.state
    record = await state.sessions.load(session_id)
    if record is None:
        return HTMLResponse("会话不存在", status_code=404)
    loaded = await state.sessions.load_state(session_id)
    st = loaded[0] if loaded else None
    budget = st.risk_budget if st else 1.0
    plans = await state.plans.list_by_session(session_id)
    entries = await state.audit.list_entries(session_id)
    timeline = [{
        "seq": e.seq, "tool": e.tool, "decision": e.decision,
        "reasons": e.reasons, "hash8": e.entry_hash[:8],
        "timestamp": e.timestamp,
    } for e in entries]
    return templates.TemplateResponse(request, "session_detail.html", {
        "record": record, "budget": budget,
        "color": _budget_color(budget), "width": _budget_width(budget),
        "flagged": st.flagged if st else False,
        "entities": st.entities if st else {},
        "taint": sorted(st.taint) if st else [],
        "plans": plans, "timeline": timeline,
    })


@router.get("/console/plans/{plan_id}", response_class=HTMLResponse)
async def console_plan(request: Request, plan_id: str) -> HTMLResponse:
    state = request.app.state
    plan = await state.plans.load(plan_id)
    if plan is None:
        return HTMLResponse("计划不存在", status_code=404)
    projected: dict = {}
    if plan.projected_json:
        projected = json.loads(plan.projected_json)
    metrics = projected.get("metrics", {})
    delta = projected.get("delta", {})
    return templates.TemplateResponse(request, "plan_detail.html", {
        "plan": plan, "metrics": metrics, "delta": delta,
        "action_hashes": [h[:8] for h in
                          (plan.token().action_hashes if plan.token() else [])],
    })


@router.post("/console/plans/{plan_id}/resolve")
async def console_resolve(
    request: Request, plan_id: str, resolution: str = Form(...),
    decided_by: str = Form("console"),
    _csrf: None = Depends(require_csrf),
) -> RedirectResponse:
    authenticated_approver = getattr(request.state, "authenticated_approver", None)
    await resolve_plan_once(
        request.app.state, plan_id, resolution,
        authenticated_approver or decided_by, None,
    )
    return RedirectResponse(f"/console/plans/{plan_id}", status_code=303)
