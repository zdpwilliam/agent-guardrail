from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from guardrail.api.auth import require_actor
from guardrail.audit import ChainVerdict

router = APIRouter(
    prefix="/v1", tags=["audit"], dependencies=[Depends(require_actor)]
)


@router.get("/audit/verify", response_model=ChainVerdict)
async def verify_audit(request: Request) -> ChainVerdict:
    """校验整条审计链。

    这是「审计链可验证」这件事最早的对外证据：控制台（M5）之前，先有一个
    能跑的 HTTP 入口。控制台做出来之后它会退居幕后，但不会消失——
    CI 的 `make verify` 会调它。
    """
    return await request.app.state.audit.verify_chain()
