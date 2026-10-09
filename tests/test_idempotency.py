import pytest

from guardrail.idempotency import idempotency_key
from guardrail.stores.idempotency import SqliteIdempotencyStore
from guardrail.stores.sqlite import SqliteBackend

ARGS = {"product_id": "p-1", "delta_pct": -5.0}


@pytest.fixture
async def store(tmp_path):
    backend = SqliteBackend(str(tmp_path / "gateway.db"))
    await backend.connect()
    yield SqliteIdempotencyStore(backend)
    await backend.close()


# ---------- 键 ----------


def test_key_is_stable_across_calls():
    assert idempotency_key("s-1", "update_price", ARGS) == idempotency_key(
        "s-1", "update_price", dict(ARGS)
    )


def test_key_ignores_arg_order():
    a = idempotency_key("s-1", "t", {"x": 1, "y": 2})
    b = idempotency_key("s-1", "t", {"y": 2, "x": 1})
    assert a == b


def test_key_varies_by_session():
    assert idempotency_key("s-1", "t", ARGS) != idempotency_key("s-2", "t", ARGS)


def test_key_varies_by_tool():
    assert idempotency_key("s-1", "update_price", ARGS) != idempotency_key(
        "s-1", "update_stock", ARGS
    )


def test_key_varies_by_args():
    assert idempotency_key("s-1", "t", {"a": 1}) != idempotency_key("s-1", "t", {"a": 2})


def test_key_is_hex_sha256():
    key = idempotency_key("s-1", "t", ARGS)
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_key_has_no_field_delimiter_ambiguity():
    # 拼接式哈希（session ‖ tool ‖ json）会碰撞：("ab","c",{}) 与 ("a","bc",{})
    # 拼出同一个字符串。整体 canonical_json 之后哈希就没有这个问题。
    assert idempotency_key("ab", "c", {}) != idempotency_key("a", "bc", {})


# ---------- 三种命中情况 ----------


async def test_miss_then_begin_succeeds(store):
    assert await store.get(idempotency_key("s-1", "t", ARGS)) is None
    assert await store.begin(idempotency_key("s-1", "t", ARGS), "s-1") is True


async def test_in_progress_blocks_second_begin(store):
    key = idempotency_key("s-1", "t", ARGS)
    assert await store.begin(key, "s-1") is True
    assert await store.begin(key, "s-1") is False
    record = await store.get(key)
    assert record is not None
    assert record.status == "in_progress"


async def test_done_stores_response(store):
    key = idempotency_key("s-1", "t", ARGS)
    await store.begin(key, "s-1")
    await store.complete(key, {"tool": "t", "result": {"ok": True}})
    record = await store.get(key)
    assert record is not None
    assert record.status == "done"
    assert record.response == {"tool": "t", "result": {"ok": True}}


async def test_completed_key_cannot_be_reclaimed(store):
    # 一个已经完成的键必须永远返回 replay，不能被后来的 begin 抢走变成
    # 「重新执行」——那等于让重试产生第二次副作用。
    key = idempotency_key("s-1", "t", ARGS)
    await store.begin(key, "s-1")
    await store.complete(key, {"x": 1})
    assert await store.begin(key, "s-1") is False
    record = await store.get(key)
    assert record is not None
    assert record.status == "done"
    assert record.response == {"x": 1}


async def test_concurrent_begin_lets_exactly_one_win(store):
    # 「先查再插」在并发下有窗口：两个请求都查到不存在，然后都插入。
    import asyncio

    key = idempotency_key("s-1", "t", ARGS)
    results = await asyncio.gather(*(store.begin(key, "s-1") for _ in range(5)))
    assert results.count(True) == 1


async def test_release_allows_retry(store):
    # 执行失败时必须释放：否则一次 409 就会把这个键在会话 TTL 内永久堵死，
    # 而那次调用其实什么也没做成。
    key = idempotency_key("s-1", "t", ARGS)
    await store.begin(key, "s-1")
    await store.release(key)
    assert await store.get(key) is None
    assert await store.begin(key, "s-1") is True


async def test_release_on_missing_key_is_noop(store):
    await store.release("nonexistent")


async def test_begin_records_session_id(store):
    key = idempotency_key("s-1", "t", ARGS)
    await store.begin(key, "s-1")
    record = await store.get(key)
    assert record is not None
    assert record.session_id == "s-1"


async def test_complete_on_missing_key_raises(store):
    # 静默成功会让「忘记 begin」的 bug 消失在日志里。
    with pytest.raises(RuntimeError):
        await store.complete("nonexistent", {"x": 1})
