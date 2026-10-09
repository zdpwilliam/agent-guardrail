from __future__ import annotations

import asyncio
import json

import aiosqlite

from guardrail.audit import (
    GENESIS_HASH,
    AuditDraft,
    AuditEntry,
    ChainVerdict,
    compute_entry_hash,
    redact_sensitive,
    sha256_hex,
)
from guardrail.stores.sqlite import SqliteBackend


class SqliteAuditSink:
    """SQLite 哈希链。

    append 在进程内用一把 asyncio 锁串行化。理由不是「SQLite 慢」，而是
    「读 prev_hash → 算 hash → 插入」这三步必须不可交错：两个并发的 append
    会读到同一个 prev_hash，链条从此断裂。锁的范围只覆盖这三步。
    """

    def __init__(self, backend: SqliteBackend) -> None:
        self.backend = backend
        self._lock = asyncio.Lock()

    async def append(self, draft: AuditDraft) -> AuditEntry:
        async with self._lock:
            prev_hash = await self._head_hash_unlocked()
            payload = draft.model_dump()
            payload["args"] = redact_sensitive(payload["args"])
            entry_hash = compute_entry_hash(prev_hash, payload)
            cur = await self.backend.execute(
                "INSERT INTO audit_log"
                " (prev_hash, entry_hash, session_id, plan_id, tool, args_json,"
                "  decision, replay, reasons_json, timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    prev_hash,
                    entry_hash,
                    draft.session_id,
                    draft.plan_id,
                    draft.tool,
                    json.dumps(payload["args"], ensure_ascii=False),
                    draft.decision,
                    1 if draft.replay else 0,
                    json.dumps(draft.reasons, ensure_ascii=False),
                    draft.timestamp,
                ),
            )
            await self.backend.commit()
            seq = cur.lastrowid
        if seq is None:  # pragma: no cover - aiosqlite 一定会填 lastrowid
            raise RuntimeError("审计链插入后拿不到 seq")
        return AuditEntry(seq=seq, prev_hash=prev_hash, entry_hash=entry_hash, **payload)

    async def _head_hash_unlocked(self) -> str:
        row = await self.backend.fetchone(
            "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        )
        return row["entry_hash"] if row is not None else GENESIS_HASH

    async def head_hash(self) -> str:
        return await self._head_hash_unlocked()

    async def head(self) -> tuple[int, str]:
        row = await self.backend.fetchone(
            "SELECT seq, entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        )
        if row is None:
            return 0, GENESIS_HASH
        return row["seq"], row["entry_hash"]

    async def verify_anchor(self, seq: int, entry_hash: str) -> ChainVerdict:
        """用外部保存的链头校验尾部是否被截断。

        `verify_chain()` 只能校验现存前缀自洽；一旦最后一条被删掉，前缀仍然
        看起来完整。外部锚点把「我在某个时刻看到的 seq 和 hash」带回来，才能
        把「当前链短于锚点」或「锚点位置哈希已变」识别为篡改。
        """
        row = await self.backend.fetchone(
            "SELECT entry_hash FROM audit_log WHERE seq = ?", (seq,)
        )
        if row is None:
            return ChainVerdict(
                ok=False,
                checked=0,
                broken_at_seq=seq,
                reason=f"外部锚点 seq={seq} 在审计链中不存在，尾部可能被截断",
            )
        if row["entry_hash"] != entry_hash:
            return ChainVerdict(
                ok=False,
                checked=0,
                broken_at_seq=seq,
                reason=(
                    f"外部锚点 seq={seq} 的哈希与当前审计链不符："
                    f"当前 {row['entry_hash'][:8]}，锚点 {entry_hash[:8]}"
                ),
            )
        return await self.verify_chain()

    async def list_entries(self, session_id: str, limit: int = 200) -> list[AuditEntry]:
        rows = await self.backend.fetchall(
            "SELECT * FROM audit_log WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
            (session_id, limit),
        )
        return [_to_entry(r) for r in rows]

    async def verify_chain(self, since_seq: int = 0) -> ChainVerdict:
        rows = await self.backend.fetchall(
            "SELECT * FROM audit_log WHERE seq >= ? ORDER BY seq", (since_seq,)
        )
        if not rows:
            return ChainVerdict(ok=True, checked=0)

        if since_seq > 0:
            prior = await self.backend.fetchone(
                "SELECT entry_hash FROM audit_log WHERE seq = ?", (since_seq - 1,)
            )
            expected_prev = prior["entry_hash"] if prior is not None else GENESIS_HASH
        else:
            expected_prev = GENESIS_HASH
        cursor = rows[0]["seq"]

        for row in rows:
            entry = _to_entry(row)

            # seq 连续性。缺这一条，「删掉中间某条」会只表现为「后面某条的
            # prev_hash 悬空」——能发现，但报出来的 seq 指向的是受害者而不是
            # 真正被删的那条。显式检查让定位直接指向缺口。
            if entry.seq != cursor:
                return ChainVerdict(
                    ok=False,
                    checked=len(rows),
                    broken_at_seq=entry.seq,
                    reason=(
                        f"seq={entry.seq} 序列不连续：期望 seq={cursor}，"
                        f"说明有记录被删除或插入"
                    ),
                )

            if entry.prev_hash != expected_prev:
                return ChainVerdict(
                    ok=False,
                    checked=len(rows),
                    broken_at_seq=entry.seq,
                    reason=(
                        f"seq={entry.seq} 的 prev_hash 与前一条不符："
                        f"记录 {entry.prev_hash[:8]}，实际应为 {expected_prev[:8]}"
                    ),
                )
            recomputed = compute_entry_hash(entry.prev_hash, entry.hashed_payload())
            if recomputed != entry.entry_hash:
                return ChainVerdict(
                    ok=False,
                    checked=len(rows),
                    broken_at_seq=entry.seq,
                    reason=(
                        f"seq={entry.seq} 的 payload 已被篡改："
                        f"重算得 {recomputed[:8]}，记录为 {entry.entry_hash[:8]}"
                    ),
                )
            expected_prev = entry.entry_hash
            cursor += 1

        return ChainVerdict(ok=True, checked=len(rows))

    async def execute_tamper_for_test(
        self,
        seq: int,
        *,
        args_json: str | None = None,
        decision: str | None = None,
        delete: bool = False,
        forged: bool = False,
    ) -> None:
        """直接改写底层存储——**只给测试用**。

        审计链的验收标准是「篡改必须被发现」（spec §7），而篡改按定义不能
        经过 AuditSink 的公开接口。这个方法就是那条「后门」，因此它的存在本身
        就是「哈希链只防外部篡改、不防有写库权限的人」这一事实的证据——
        README 与 docs/limitations.md 要写明这一点。
        """
        if delete:
            await self.backend.execute("DELETE FROM audit_log WHERE seq = ?", (seq,))
            await self.backend.commit()
            return
        if args_json is not None:
            await self.backend.execute(
                "UPDATE audit_log SET args_json = ? WHERE seq = ?", (args_json, seq)
            )
        if decision is not None:
            await self.backend.execute(
                "UPDATE audit_log SET decision = ? WHERE seq = ?", (decision, seq)
            )
        if forged:
            await self.backend.execute(
                "UPDATE audit_log SET entry_hash = ? WHERE seq = ?",
                (sha256_hex("forged"), seq),
            )
        await self.backend.commit()


def _to_entry(row: aiosqlite.Row) -> AuditEntry:
    return AuditEntry(
        seq=row["seq"],
        prev_hash=row["prev_hash"],
        entry_hash=row["entry_hash"],
        session_id=row["session_id"],
        plan_id=row["plan_id"],
        tool=row["tool"],
        args=json.loads(row["args_json"]),
        decision=row["decision"],
        replay=bool(row["replay"]),
        reasons=json.loads(row["reasons_json"]),
        timestamp=row["timestamp"],
    )
