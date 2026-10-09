from __future__ import annotations

import json
from typing import Any

from guardrail.audit import canonical_json
from guardrail.clock import now_iso
from guardrail.idempotency import IdempotencyRecord
from guardrail.stores.sqlite import SqliteBackend


class SqliteIdempotencyStore:
    def __init__(self, backend: SqliteBackend) -> None:
        self.backend = backend

    async def get(self, key: str) -> IdempotencyRecord | None:
        row = await self.backend.fetchone("SELECT * FROM idempotency WHERE key = ?", (key,))
        if row is None:
            return None
        return IdempotencyRecord(
            key=row["key"],
            session_id=row["session_id"],
            status=row["status"],
            response=json.loads(row["response_json"]) if row["response_json"] else None,
            created_at=row["created_at"],
        )

    async def begin(self, key: str, session_id: str) -> bool:
        """登记一次进行中的调用。已被占用时返回 False。

        实现要点：先无条件占位插入（状态写 `done`、响应为空），再用一次带
        status 条件的 UPDATE 认领。两个细节都是必要的：

        - 「先查再插」在并发下有窗口。两个请求可能都查到「不存在」，然后都插入。
        - 占位时写 `done` 而不是 `in_progress`，认领时才改成 `in_progress`：这样
          一个**已完成**的键（`done` + 响应非空）永远不会被后来的 `begin` 抢走，
          而 `complete()` 推进到的 `done` 状态带非空响应，两种 `done` 不会混淆。
        """
        await self.backend.execute(
            "INSERT OR IGNORE INTO idempotency"
            " (key, session_id, status, response_json, created_at)"
            " VALUES (?, ?, 'done', NULL, ?)",
            (key, session_id, now_iso()),
        )
        cur = await self.backend.execute(
            "UPDATE idempotency SET status = 'in_progress', created_at = ?"
            " WHERE key = ? AND status = 'done' AND response_json IS NULL",
            (now_iso(), key),
        )
        await self.backend.commit()
        return (cur.rowcount or 0) > 0

    async def complete(self, key: str, response: dict[str, Any]) -> None:
        cur = await self.backend.execute(
            "UPDATE idempotency SET status = 'done', response_json = ? WHERE key = ?",
            (canonical_json(response), key),
        )
        await self.backend.commit()
        if cur.rowcount == 0:
            raise RuntimeError(f"幂等键未登记就 complete：{key}")

    async def release(self, key: str) -> None:
        await self.backend.execute("DELETE FROM idempotency WHERE key = ?", (key,))
        await self.backend.commit()
