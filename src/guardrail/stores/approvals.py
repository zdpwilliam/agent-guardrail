from __future__ import annotations

import json
import uuid

from guardrail.clock import now_iso
from guardrail.protocols import PendingApproval
from guardrail.stores.sqlite import SqliteBackend


class SqliteApprovalStore:
    def __init__(self, backend: SqliteBackend) -> None:
        self.backend = backend

    async def create(self, approval: PendingApproval) -> PendingApproval:
        if not approval.id:
            approval = approval.model_copy(
                update={"id": f"pa-{uuid.uuid4().hex[:12]}"}
            )
        await self.backend.execute(
            "INSERT INTO pending_approvals"
            " (id, session_id, agent_id, tool, args_json, reasons_json, cost, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                approval.id, approval.session_id, approval.agent_id, approval.tool,
                json.dumps(approval.args, ensure_ascii=False),
                json.dumps(approval.reasons, ensure_ascii=False),
                approval.cost, approval.created_at,
            ),
        )
        await self.backend.commit()
        return approval

    async def load(self, approval_id: str) -> PendingApproval | None:
        row = await self.backend.fetchone(
            "SELECT * FROM pending_approvals WHERE id = ?", (approval_id,)
        )
        return _to_approval(row) if row is not None else None

    async def resolve(
        self, approval_id: str, resolution: str, decided_by: str,
        comment: str | None = None,
    ) -> PendingApproval | None:
        """原子认领：条件更新把「查了再改」的窗口关掉——两个审批人同时点，
        只有一个成功，另一个拿到 None（已处理）。"""
        cur = await self.backend.execute(
            "UPDATE pending_approvals"
            " SET resolved_at = ?, resolution = ?, decided_by = ?, comment = ?"
            " WHERE id = ? AND resolved_at IS NULL",
            (now_iso(), resolution, decided_by, comment, approval_id),
        )
        await self.backend.commit()
        if (cur.rowcount or 0) == 0:
            return None
        return await self.load(approval_id)

    async def list_open(self, limit: int = 50) -> list[PendingApproval]:
        rows = await self.backend.fetchall(
            "SELECT * FROM pending_approvals WHERE resolved_at IS NULL"
            " ORDER BY created_at LIMIT ?",
            (limit,),
        )
        return [_to_approval(r) for r in rows]


def _to_approval(row) -> PendingApproval:  # noqa: ANN001 - aiosqlite.Row
    return PendingApproval(
        id=row["id"],
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        tool=row["tool"],
        args=json.loads(row["args_json"]),
        reasons=json.loads(row["reasons_json"]),
        cost=row["cost"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        resolution=row["resolution"],
        decided_by=row["decided_by"],
        comment=row["comment"],
    )
