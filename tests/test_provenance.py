import pytest

from guardrail.models import EntityEmit, ToolSpec
from guardrail.provenance import (
    check_requirements,
    emitted_entity_ids,
    register_result,
)
from guardrail.stores.provenance import SqliteProvenanceStore
from guardrail.stores.sqlite import SqliteBackend
from guardrail.tools.registry import TOOL_SPECS


@pytest.fixture
async def store(tmp_path):
    backend = SqliteBackend(str(tmp_path / "gateway.db"))
    await backend.connect()
    yield SqliteProvenanceStore(backend)
    await backend.close()


def _spec(**kwargs) -> ToolSpec:
    base = {"name": "t", "kind": "write", "args_schema": {"type": "object", "properties": {}}}
    return ToolSpec(**{**base, **kwargs})


# ---------- 路径解析 ----------


def test_emits_reads_nested_key():
    spec = _spec(emits=[EntityEmit(entity_type="product", path="product.id")])
    assert emitted_entity_ids(spec, {"product": {"id": "p-1"}}) == [("product", "p-1")]


def test_emits_reads_top_level_scalar():
    spec = _spec(emits=[EntityEmit(entity_type="coupon", path="coupon_id")])
    assert emitted_entity_ids(spec, {"coupon_id": "c-1"}) == [("coupon", "c-1")]


def test_emits_reads_list_wildcard():
    spec = _spec(emits=[EntityEmit(entity_type="product", path="products[].id")])
    result = {"products": [{"id": "p-1"}, {"id": "p-2"}, {"id": "p-3"}]}
    assert emitted_entity_ids(spec, result) == [
        ("product", "p-1"),
        ("product", "p-2"),
        ("product", "p-3"),
    ]


def test_emits_tolerates_missing_path():
    spec = _spec(emits=[EntityEmit(entity_type="product", path="product.id")])
    assert emitted_entity_ids(spec, {"order": {}}) == []


def test_emits_drops_non_string_ids():
    spec = _spec(emits=[EntityEmit(entity_type="product", path="product.id")])
    assert emitted_entity_ids(spec, {"product": {"id": 42}}) == []


def test_emits_drops_empty_string_ids():
    spec = _spec(emits=[EntityEmit(entity_type="product", path="product.id")])
    assert emitted_entity_ids(spec, {"product": {"id": ""}}) == []


def test_emits_supports_multiple_refs():
    spec = TOOL_SPECS["create_order"]
    ids = emitted_entity_ids(spec, {"order_id": "o-1", "order": {"product_id": "p-1"}})
    assert set(ids) == {("order", "o-1"), ("product", "p-1")}


# ---------- 登记 ----------


async def test_register_result_then_contains(store):
    spec = TOOL_SPECS["get_product"]
    await register_result(store, "s-1", spec, {"product": {"id": "p-1"}})
    assert await store.contains("s-1", "product", "p-1")


async def test_provenance_is_session_scoped(store):
    spec = TOOL_SPECS["get_product"]
    await register_result(store, "s-1", spec, {"product": {"id": "p-1"}})
    assert not await store.contains("s-2", "product", "p-1")


async def test_register_is_idempotent(store):
    spec = TOOL_SPECS["get_product"]
    for _ in range(3):
        await register_result(store, "s-1", spec, {"product": {"id": "p-1"}})
    rows = await store.backend.fetchall("SELECT * FROM provenance")
    assert len(rows) == 1


# ---------- 校验 ----------


async def test_requirement_satisfied_after_read(store):
    spec = TOOL_SPECS["update_price"]
    assert await check_requirements(store, "s-1", spec, {"product_id": "p-1"}) is not None
    await register_result(store, "s-1", TOOL_SPECS["get_product"], {"product": {"id": "p-1"}})
    assert await check_requirements(store, "s-1", spec, {"product_id": "p-1"}) is None


async def test_requirement_names_the_offending_entity(store):
    reason = await check_requirements(
        store, "s-1", TOOL_SPECS["update_price"], {"product_id": "p-evil"}
    )
    assert "product" in reason
    assert "p-evil" in reason
    assert "s-1" in reason


async def test_entity_from_another_session_is_rejected(store):
    await register_result(store, "s-1", TOOL_SPECS["get_product"], {"product": {"id": "p-1"}})
    assert await check_requirements(store, "s-2", TOOL_SPECS["update_price"], {"product_id": "p-1"})


async def test_optional_requirement_absent_is_fine(store):
    spec = TOOL_SPECS["create_order"]
    await register_result(store, "s-1", TOOL_SPECS["get_product"], {"product": {"id": "p-1"}})
    assert await check_requirements(store, "s-1", spec, {"product_id": "p-1"}) is None


async def test_optional_requirement_present_must_be_known(store):
    spec = TOOL_SPECS["create_order"]
    await register_result(store, "s-1", TOOL_SPECS["get_product"], {"product": {"id": "p-1"}})
    reason = await check_requirements(
        store, "s-1", spec, {"product_id": "p-1", "coupon_id": "c-forged"}
    )
    assert reason is not None
    assert "c-forged" in reason


async def test_read_tool_has_no_requirements_to_check(store):
    assert (
        await check_requirements(store, "s-1", TOOL_SPECS["get_order"], {"order_id": "o-9"}) is None
    )


async def test_write_tool_without_requirements_passes(store):
    # create_coupon 不引用任何已有实体——它创造新东西，不需要 provenance。
    assert (
        await check_requirements(store, "s-1", TOOL_SPECS["create_coupon"], {"code": "X"}) is None
    )
