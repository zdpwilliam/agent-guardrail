import json

import pytest

from guardrail.audit import (
    GENESIS_HASH,
    AuditDraft,
    canonical_json,
    compute_entry_hash,
    sha256_hex,
)
from guardrail.stores.audit import SqliteAuditSink
from guardrail.stores.sqlite import SqliteBackend


@pytest.fixture
async def sink(tmp_path):
    backend = SqliteBackend(str(tmp_path / "gateway.db"))
    await backend.connect()
    yield SqliteAuditSink(backend)
    await backend.close()


def _draft(tool: str = "update_price", **kwargs) -> AuditDraft:
    base = {
        "session_id": "s-1",
        "plan_id": None,
        "tool": tool,
        "args": {"product_id": "p-1", "delta_pct": -5.0},
        "decision": "allow",
        "replay": False,
        "reasons": [],
        "timestamp": "2026-10-04T10:00:00+00:00",
    }
    return AuditDraft(**{**base, **kwargs})


# ---------- 规范化与哈希 ----------


def test_canonical_json_is_key_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_keeps_chinese_readable():
    assert "单次降价" in canonical_json({"message": "单次降价不得超过 10%"})


def test_canonical_json_has_no_padding():
    assert canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'


def test_entry_hash_depends_on_prev_hash():
    a = compute_entry_hash(GENESIS_HASH, {"x": 1})
    b = compute_entry_hash("f" * 64, {"x": 1})
    assert a != b


def test_entry_hash_is_sha256_of_prev_plus_payload():
    assert compute_entry_hash("a" * 64, {"x": 1}) == sha256_hex('a' * 64 + '{"x":1}')


def test_genesis_hash_is_64_zeros():
    assert GENESIS_HASH == "0" * 64


# ---------- 追加 ----------


async def test_first_entry_chains_from_genesis(sink):
    entry = await sink.append(_draft())
    assert entry.seq == 1
    assert entry.prev_hash == GENESIS_HASH
    assert len(entry.entry_hash) == 64


async def test_entries_chain_to_each_other(sink):
    first = await sink.append(_draft())
    second = await sink.append(_draft(tool="update_stock"))
    assert second.prev_hash == first.entry_hash


async def test_hash_is_recomputable_from_stored_fields(sink):
    entry = await sink.append(_draft())
    assert compute_entry_hash(entry.prev_hash, entry.hashed_payload()) == entry.entry_hash


async def test_hashed_payload_excludes_hash_fields(sink):
    entry = await sink.append(_draft())
    payload = entry.hashed_payload()
    assert "entry_hash" not in payload
    assert "prev_hash" not in payload
    assert "seq" not in payload


async def test_head_hash_reflects_latest(sink):
    assert await sink.head_hash() == GENESIS_HASH
    entry = await sink.append(_draft())
    assert await sink.head_hash() == entry.entry_hash


async def test_list_entries_filters_by_session(sink):
    await sink.append(_draft(session_id="s-1"))
    await sink.append(_draft(session_id="s-2"))
    entries = await sink.list_entries("s-1")
    assert [e.session_id for e in entries] == ["s-1"]


async def test_list_entries_is_newest_first(sink):
    first = await sink.append(_draft(tool="update_price"))
    second = await sink.append(_draft(tool="update_stock"))
    entries = await sink.list_entries("s-1")
    assert [e.seq for e in entries] == [second.seq, first.seq]


async def test_list_entries_respects_limit(sink):
    for _ in range(5):
        await sink.append(_draft())
    assert len(await sink.list_entries("s-1", limit=2)) == 2


async def test_deny_entries_are_recorded(sink):
    entry = await sink.append(_draft(decision="deny", reasons=["单次降价不得超过 10%"]))
    assert entry.decision == "deny"
    assert entry.reasons == ["单次降价不得超过 10%"]


async def test_replay_entries_are_recorded(sink):
    entry = await sink.append(_draft(replay=True))
    assert entry.replay is True


async def test_concurrent_appends_keep_chain_intact(sink):
    # 锁只覆盖「读 prev → 算 hash → 插入」这三步。不加锁的话两个并发 append
    # 会读到同一个 prev_hash，链条从此断裂——这是本层最容易被忽略的并发点。
    import asyncio

    await asyncio.gather(*(sink.append(_draft(tool=f"t{i}")) for i in range(5)))
    verdict = await sink.verify_chain()
    assert verdict.ok is True
    assert verdict.checked == 5


# ---------- 校验 ----------


async def test_empty_chain_verifies(sink):
    verdict = await sink.verify_chain()
    assert verdict.ok is True
    assert verdict.checked == 0


async def test_valid_chain_verifies(sink):
    for _ in range(3):
        await sink.append(_draft())
    verdict = await sink.verify_chain()
    assert verdict.ok is True
    assert verdict.checked == 3
    assert verdict.broken_at_seq is None


async def test_tampered_payload_breaks_chain(sink):
    await sink.append(_draft())
    await sink.append(_draft(tool="update_stock"))
    await sink.execute_tamper_for_test(seq=1, args_json='{"product_id":"p-evil"}')
    verdict = await sink.verify_chain()
    assert verdict.ok is False
    assert verdict.broken_at_seq == 1
    assert "1" in (verdict.reason or "")


async def test_tampered_decision_breaks_chain(sink):
    await sink.append(_draft(decision="deny"))
    await sink.execute_tamper_for_test(seq=1, decision="allow")
    verdict = await sink.verify_chain()
    assert verdict.ok is False
    assert verdict.broken_at_seq == 1


async def test_deleted_middle_entry_breaks_chain(sink):
    # 中间条目被删 → 后一条的 prev_hash 悬空 → 立刻暴露。
    for _ in range(3):
        await sink.append(_draft())
    await sink.execute_tamper_for_test(seq=2, delete=True)
    verdict = await sink.verify_chain()
    assert verdict.ok is False
    assert verdict.broken_at_seq == 3


async def test_forged_middle_entry_breaks_chain(sink):
    # 攻击者能写库，但算不出正确的 entry_hash。
    for _ in range(3):
        await sink.append(_draft())
    await sink.execute_tamper_for_test(seq=2, forged=True)
    verdict = await sink.verify_chain()
    assert verdict.ok is False
    assert verdict.broken_at_seq == 2


async def test_truncating_the_tail_is_not_detectable_without_anchor(sink):
    """**已知的边界，不是缺陷。**

    删掉（或伪造）**最后一条**记录，剩下��前缀本身自洽，`verify_chain()` 会返回
    ok。要发现它需要一个本设计刻意不引入的东西：把链头哈希写到别处（WORM 存储、
    外部日志、或者干脆定期打快照）。

    spec §7 明确写了这一层「只证明『没被改』，不证明『写的是真的』」——尾部
    截断正是那句话的一个具体实例。这条测试把边界钉死：将来若有人给 verify_chain
    加上外部锚点，这条会失败，那正是应该更新文档的信号。
    """
    await sink.append(_draft())
    await sink.append(_draft())
    await sink.execute_tamper_for_test(seq=2, delete=True)
    verdict = await sink.verify_chain()
    assert verdict.ok is True
    assert verdict.checked == 1


async def test_verify_chain_since_seq_skips_earlier_entries(sink):
    for _ in range(3):
        await sink.append(_draft())
    verdict = await sink.verify_chain(since_seq=3)
    assert verdict.ok is True
    assert verdict.checked == 1


async def test_verify_chain_since_seq_beyond_head_is_ok(sink):
    await sink.append(_draft())
    verdict = await sink.verify_chain(since_seq=99)
    assert verdict.ok is True
    assert verdict.checked == 0


# ---------- 隐私与外部锚点 ----------


async def test_sensitive_fields_are_redacted_before_storage(sink):
    entry = await sink.append(
        _draft(
            args={
                "product_id": "p-1",
                "email": "alice@example.com",
                "to": "bob@example.com",
                "body": "account secret",
                "nested": [
                    {"phone": "13800138000", "beneficiary_account": "622200001111"},
                    {"content": "wire content", "recipientEmail": "carol@example.com"},
                ],
            }
        )
    )

    assert entry.args["product_id"] == "p-1"
    assert entry.args["email"] == "[REDACTED]"
    assert entry.args["to"] == "[REDACTED]"
    assert entry.args["body"] == "[REDACTED]"
    assert entry.args["nested"][0]["phone"] == "[REDACTED]"
    assert entry.args["nested"][0]["beneficiary_account"] == "[REDACTED]"
    assert entry.args["nested"][1]["content"] == "[REDACTED]"
    assert entry.args["nested"][1]["recipientEmail"] == "[REDACTED]"

    row = await sink.backend.fetchone(
        "SELECT args_json FROM audit_log WHERE seq = ?", (entry.seq,)
    )
    stored = row["args_json"]
    assert "alice@example.com" not in stored
    assert "13800138000" not in stored
    assert json.loads(stored) == entry.args
    assert compute_entry_hash(entry.prev_hash, entry.hashed_payload()) == entry.entry_hash


async def test_external_anchor_detects_tail_truncation(sink):
    await sink.append(_draft())
    latest = await sink.append(_draft(tool="update_stock"))
    anchored_seq, anchored_hash = await sink.head()

    await sink.execute_tamper_for_test(seq=latest.seq, delete=True)

    verdict = await sink.verify_anchor(anchored_seq, anchored_hash)
    assert verdict.ok is False
    assert verdict.broken_at_seq == anchored_seq
    assert "锚点" in (verdict.reason or "")


async def test_external_anchor_matches_untampered_head(sink):
    entry = await sink.append(_draft())
    seq, head_hash = await sink.head()
    verdict = await sink.verify_anchor(seq, head_hash)
    assert verdict.ok is True
    assert verdict.checked == 1
    assert head_hash == entry.entry_hash
