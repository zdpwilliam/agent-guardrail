"""可观测性（spec §17 M9）：/metrics + /readyz。

指标从审计库** scrape 时聚合**而不是进程内累加器——单进程下两者等价，
但 scrape 聚合在网关重启后数字仍在，且天然与审计链一致（spec §18.2
「不留进程内单例缓存」的同一立场）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from guardrail.api.auth import require_actor

router = APIRouter(tags=["observability"])


@router.get("/healthz")
async def healthz() -> dict:
    """进程存活探针：只证明进程在响应，不检查外部依赖。"""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> dict:
    """就绪探针：DB 连通 + 三份策略已装载。任一失败 → 503。"""
    state = request.app.state
    problems = []
    try:
        await state.backend.fetchone("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - 探针要报告任何失败原因
        problems.append(f"db: {exc}")
    if getattr(state, "policy", None) is None:
        problems.append("policy 未装载")
    if getattr(state, "combined_policy", None) is None:
        problems.append("combined_policy 未装载")
    if getattr(state, "plan_policy", None) is None:
        problems.append("plan_policy 未装载")
    try:
        downstream = await state.shop.get("/readyz")
        if downstream.status_code != 200:
            problems.append(f"shop: HTTP {downstream.status_code}")
    except Exception as exc:  # noqa: BLE001 - 探针要报告下游失败原因
        problems.append(f"shop: {exc}")
    if problems:
        return JSONResponse(
            content={"status": "unavailable", "problems": problems},
            status_code=503,
        )
    return {"status": "ok"}


@router.get("/metrics")
async def metrics(
    request: Request,
    authenticated_actor: tuple[str, str] | None = Depends(require_actor),
) -> Response:
    """Prometheus 文本格式。能回答「这次为什么被拒」的计数面：
    按决策与按拒绝理由的条目数。"""
    state = request.app.state
    rows = await state.backend.fetchall(
        "SELECT decision, COUNT(*) AS n FROM audit_log GROUP BY decision"
    )
    reasons = await state.backend.fetchall(
        "SELECT reasons_json, COUNT(*) AS n FROM audit_log"
        " WHERE decision IN ('deny', 'ask') GROUP BY reasons_json ORDER BY n DESC LIMIT 10"
    )
    lines = [
        "# HELP guardrail_audit_entries_total 网关审计条目数（按决策）",
        "# TYPE guardrail_audit_entries_total counter",
    ]
    total = 0
    for r in rows:
        lines.append(f'guardrail_audit_entries_total{{decision="{r["decision"]}"}} {r["n"]}')
        total += r["n"]
    lines.append(f"guardrail_audit_entries_total {total}")
    lines.append("# HELP guardrail_denial_reasons_total 拒绝/扣下原因 TOP10")
    lines.append("# TYPE guardrail_denial_reasons_total counter")
    import json

    for r in reasons:
        for reason in json.loads(r["reasons_json"]):
            if reason:
                safe = reason.replace("\\", "\\\\").replace('"', "'").replace("\n", " ")
                lines.append(f'guardrail_denial_reasons_total{{reason="{safe}"}} {r["n"]}')
    return Response(content="\n".join(lines) + "\n", media_type="text/plain")
