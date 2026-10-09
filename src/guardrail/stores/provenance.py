from __future__ import annotations

from guardrail.clock import now_iso
from guardrail.stores.sqlite import SqliteBackend


class SqliteProvenanceStore:
    def __init__(self, backend: SqliteBackend) -> None:
        self.backend = backend

    async def register(self, session_id: str, refs: list[tuple[str, str]]) -> None:
        issued_at = now_iso()
        await self.backend.executemany(
            "INSERT OR IGNORE INTO provenance (session_id, entity_type, entity_id, issued_at)"
            " VALUES (?, ?, ?, ?)",
            [(session_id, entity_type, entity_id, issued_at) for entity_type, entity_id in refs],
        )
        await self.backend.commit()

    async def contains(self, session_id: str, entity_type: str, entity_id: str) -> bool:
        row = await self.backend.fetchone(
            "SELECT 1 FROM provenance"
            " WHERE session_id = ? AND entity_type = ? AND entity_id = ?",
            (session_id, entity_type, entity_id),
        )
        return row is not None
