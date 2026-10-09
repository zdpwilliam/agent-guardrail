import json

from guardrail import __main__ as cli
from guardrail.audit import AuditDraft
from guardrail.stores.audit import SqliteAuditSink
from guardrail.stores.sqlite import SqliteBackend


def _draft(tool: str = "list_products") -> AuditDraft:
    return AuditDraft(
        session_id="s-1",
        tool=tool,
        args={},
        decision="allow",
        reasons=[],
        timestamp="2026-10-09T00:00:00+00:00",
    )


async def _seed_chain(db_path) -> None:
    backend = SqliteBackend(str(db_path))
    await backend.connect()
    try:
        sink = SqliteAuditSink(backend)
        await sink.append(_draft())
        await sink.append(_draft("update_stock"))
    finally:
        await backend.close()


async def _truncate_tail(db_path) -> None:
    backend = SqliteBackend(str(db_path))
    await backend.connect()
    try:
        await SqliteAuditSink(backend).execute_tamper_for_test(2, delete=True)
    finally:
        await backend.close()


def test_audit_anchor_command_prints_jsonl(tmp_path, monkeypatch, capsys):
    db = tmp_path / "gateway.db"
    anchor_file = tmp_path / "anchors.jsonl"
    __import__("asyncio").run(_seed_chain(db))

    monkeypatch.setattr(
        "sys.argv",
        ["guardrail", "audit-anchor", "--db", str(db), "--anchor-file", str(anchor_file)],
    )
    assert cli.main() == 0
    line = anchor_file.read_text(encoding="utf-8").strip()
    payload = json.loads(line)
    assert payload["seq"] == 2
    assert len(payload["entry_hash"]) == 64
    assert capsys.readouterr().out == ""


def test_verify_command_uses_external_anchor_to_detect_truncation(
    tmp_path, monkeypatch, capsys
):
    db = tmp_path / "gateway.db"
    anchor_file = tmp_path / "anchors.jsonl"
    __import__("asyncio").run(_seed_chain(db))
    anchor_file.write_text(
        json.dumps({"seq": 2, "entry_hash": "placeholder"}) + "\n",
        encoding="utf-8",
    )
    # 让锚点保存真实链头，再删除尾部记录，模拟外部日志仍在而本地库被截断。
    monkeypatch.setattr(
        "sys.argv",
        ["guardrail", "audit-anchor", "--db", str(db), "--anchor-file", str(anchor_file)],
    )
    assert cli.main() == 0
    __import__("asyncio").run(_truncate_tail(db))

    monkeypatch.setattr(
        "sys.argv",
        ["guardrail", "verify", "--db", str(db), "--anchor-file", str(anchor_file)],
    )
    assert cli.main() == 1
    assert "尾部可能被截断" in capsys.readouterr().out


def test_maintenance_command_prints_counts(tmp_path, monkeypatch, capsys):
    db = tmp_path / "gateway.db"

    async def seed() -> None:
        backend = SqliteBackend(str(db))
        await backend.connect()
        try:
            await backend.execute(
                "INSERT INTO sessions"
                " (id, agent_id, task_id, created_at, expires_at)"
                " VALUES ('s-old', 'ops_agent', NULL, ?, ?)",
                ("2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00"),
            )
            await backend.commit()
        finally:
            await backend.close()

    __import__("asyncio").run(seed())
    monkeypatch.setattr(
        "sys.argv",
        ["guardrail", "maintenance", "--db", str(db), "--retention-days", "1"],
    )
    assert cli.main() == 0
    counts = json.loads(capsys.readouterr().out.strip())
    assert counts["sessions"] == 1
    assert counts["audit"] == 0
