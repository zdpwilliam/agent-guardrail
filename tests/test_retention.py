from guardrail.retention import purge_operational_data
from guardrail.stores.sqlite import SqliteBackend


async def _seed_retention_rows(backend: SqliteBackend) -> None:
    rows = {
        "sessions": (
            "INSERT INTO sessions"
            " (id, agent_id, task_id, created_at, expires_at)"
            " VALUES (?, 'ops_agent', 't', ?, ?)",
            [
                ("s-old", "2025-01-01T00:00:00+00:00", "2025-01-02T00:00:00+00:00"),
                ("s-live", "2026-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"),
            ],
        ),
        "plans": (
            "INSERT INTO plans"
            " (plan_id, session_id, agent_id, intent, actions_json, status,"
            "  triggered_rules_json, reasons_json, executed_hashes_json, outputs_json,"
            "  created_at)"
            " VALUES (?, 's-1', 'ops_agent', '', '[]', ?, '[]', '[]', '[]', '{}', ?)",
            [
                ("pl-old-done", "completed", "2025-01-01T00:00:00+00:00"),
                ("pl-old-pending", "pending", "2025-01-01T00:00:00+00:00"),
                ("pl-recent-done", "completed", "2099-01-01T00:00:00+00:00"),
            ],
        ),
        "pending_approvals": (
            "INSERT INTO pending_approvals"
            " (id, session_id, agent_id, tool, args_json, reasons_json, cost,"
            "  created_at, resolved_at, resolution)"
            " VALUES (?, 's-1', 'ops_agent', 'update_price', '{}', '[]', 0.1, ?, ?, ?)",
            [
                (
                    "pa-old-resolved",
                    "2025-01-01T00:00:00+00:00",
                    "2025-01-02T00:00:00+00:00",
                    "approved",
                ),
                ("pa-old-open", "2025-01-01T00:00:00+00:00", None, None),
                (
                    "pa-recent-resolved",
                    "2099-01-01T00:00:00+00:00",
                    "2099-01-02T00:00:00+00:00",
                    "approved",
                ),
            ],
        ),
    }
    for sql, params in rows.values():
        await backend.executemany(sql, params)
    await backend.commit()


async def test_retention_purges_only_terminal_old_records(tmp_path):
    backend = SqliteBackend(str(tmp_path / "gateway.db"))
    await backend.connect()
    try:
        await _seed_retention_rows(backend)
        counts = await purge_operational_data(
            backend,
            cutoff="2026-01-01T00:00:00+00:00",
            now="2026-07-01T00:00:00+00:00",
        )
        assert counts == {
            "sessions": 1,
            "plans": 1,
            "approvals": 1,
            "idempotency": 0,
            "provenance": 0,
            "audit": 0,
        }

        session_ids = {
            r["id"] for r in await backend.fetchall("SELECT id FROM sessions")
        }
        plan_ids = {
            r["plan_id"] for r in await backend.fetchall("SELECT plan_id FROM plans")
        }
        approval_ids = {
            r["id"] for r in await backend.fetchall("SELECT id FROM pending_approvals")
        }
        assert session_ids == {"s-live"}
        assert plan_ids == {"pl-old-pending", "pl-recent-done"}
        assert approval_ids == {"pa-old-open", "pa-recent-resolved"}
    finally:
        await backend.close()


async def test_retention_never_deletes_audit_chain(tmp_path):
    backend = SqliteBackend(str(tmp_path / "gateway.db"))
    await backend.connect()
    try:
        await backend.execute(
            "INSERT INTO audit_log"
            " (prev_hash, entry_hash, session_id, tool, args_json, decision,"
            "  replay, reasons_json, timestamp)"
            " VALUES (?, ?, 's-1', 'list_products', '{}', 'allow', 0, '[]', ?)",
            ("0" * 64, "a" * 64, "2025-01-01T00:00:00+00:00"),
        )
        await backend.commit()
        counts = await purge_operational_data(
            backend,
            cutoff="2026-01-01T00:00:00+00:00",
            now="2026-07-01T00:00:00+00:00",
        )
        assert counts["audit"] == 0
        rows = await backend.fetchall("SELECT seq FROM audit_log")
        assert len(rows) == 1
    finally:
        await backend.close()
