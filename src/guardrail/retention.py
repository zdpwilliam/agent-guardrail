from __future__ import annotations

from guardrail.stores.sqlite import SqliteBackend

_TERMINAL_PLAN_STATUSES = ("completed", "rejected", "expired")


async def purge_operational_data(
    backend: SqliteBackend,
    *,
    cutoff: str,
    now: str,
) -> dict[str, int]:
    """清理运维数据，但绝不删除审计链。

    会话按 expires_at 清理；计划和已解决的审批单只在进入终态后按创建/解决时间
    清理。未解决审批单和 pending 计划从保留期内保留，避免自动删除仍需人工处置
    的风险对象。审计链是 WORM 语义的追加日志，不能为了“清理”破坏哈希连续性；
    其归档与外部锚点由 `audit-anchor` / 备份流程负责。
    """
    counts: dict[str, int] = {}

    cur = await backend.execute(
        "DELETE FROM sessions WHERE expires_at <= ?",
        (now,),
    )
    counts["sessions"] = cur.rowcount or 0

    placeholders = ",".join("?" for _ in _TERMINAL_PLAN_STATUSES)
    cur = await backend.execute(
        f"DELETE FROM plans WHERE status IN ({placeholders}) AND created_at < ?",
        (*_TERMINAL_PLAN_STATUSES, cutoff),
    )
    counts["plans"] = cur.rowcount or 0

    cur = await backend.execute(
        "DELETE FROM pending_approvals"
        " WHERE resolved_at IS NOT NULL AND resolved_at < ?",
        (cutoff,),
    )
    counts["approvals"] = cur.rowcount or 0

    cur = await backend.execute(
        "DELETE FROM idempotency WHERE status = 'done' AND created_at < ?",
        (cutoff,),
    )
    counts["idempotency"] = cur.rowcount or 0

    cur = await backend.execute(
        "DELETE FROM provenance WHERE session_id NOT IN (SELECT id FROM sessions)"
    )
    counts["provenance"] = cur.rowcount or 0

    await backend.commit()
    counts["audit"] = 0
    return counts
