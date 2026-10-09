from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from guardrail.api.auth import require_agent
from guardrail.clock import now_iso
from guardrail.protocols import SessionRecord

router = APIRouter(prefix="/v1", tags=["sessions"])


class SessionCreate(BaseModel):
    agent_id: str
    task_id: str | None = None


@router.post("/sessions")
async def create_session(
    request: Request,
    body: SessionCreate,
    authenticated_agent: str | None = Depends(require_agent),
) -> dict[str, str | None]:
    """开一个会话，并顺手清掉过期的旧会话。

    没有后台调度器（单进程 demo，见 spec §18.2），所以清理挂在建会话这个低频
    写操作上。真实部署里这一步会换成定时任务，Store 接口不变。
    """
    if authenticated_agent is not None and body.agent_id != authenticated_agent:
        raise HTTPException(
            status_code=403,
            detail="认证身份与请求体中的 agent_id 不一致",
        )

    store = request.app.state.sessions
    await store.sweep_expired(now_iso())

    now = datetime.now(UTC)
    record = SessionRecord(
        session_id=f"s-{uuid.uuid4().hex[:12]}",
        agent_id=body.agent_id,
        task_id=body.task_id,
        created_at=now.isoformat(),
        expires_at=(
            now + timedelta(minutes=request.app.state.settings.session_ttl_minutes)
        ).isoformat(),
    )
    await store.save(record)
    return record.model_dump()
