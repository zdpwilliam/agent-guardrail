from __future__ import annotations

from typing import Any, Protocol

from guardrail.models import ToolSpec


class ProvenanceStore(Protocol):
    """Provenance 登记存储（spec §8）。

    对抗的是间接提示注入：被污染的模型无法凭空编造一个真实存在的商品 id
    去改价，因为它必须先在本会话读到过。
    """

    async def register(self, session_id: str, refs: list[tuple[str, str]]) -> None: ...

    async def contains(self, session_id: str, entity_type: str, entity_id: str) -> bool: ...


def _children(node: Any, key: str) -> list[Any]:  # noqa: ANN401 - 遍历任意 JSON
    """从一个 JSON 节点里取出下一层候选值。

    `key` 为空表示不再下降，当前节点本身就是候选。列表会被摊平——`[]` 通配
    就是靠这一步生效的。
    """
    if key == "":
        return [node]
    if isinstance(node, list):
        return list(node)
    if not isinstance(node, dict) or key not in node:
        return []
    value = node[key]
    return list(value) if isinstance(value, list) else [value]


def _resolve(node: Any, path: str) -> list[str]:  # noqa: ANN401 - 遍历任意 JSON
    """按受限路径语法取出实体 id 列表。

    路径不存在时返回空列表而不是抛错：工具换了返回结构时，provenance 少登记
    几个实体会让写操作被拒（fail-closed 的方向），不会让它凭空放行。
    """
    head, sep, tail = path.partition(".")
    if head.endswith("[]"):
        head = head[:-2]
        children = _children(node, head)
        return [found for child in children for found in _resolve(child, tail if sep else "")]
    children = _children(node, head)
    if sep:
        return [found for child in children for found in _resolve(child, tail)]
    return children


def emitted_entity_ids(spec: ToolSpec, result: dict[str, Any]) -> list[tuple[str, str]]:
    """从工具返回值里取出它产出的 (实体类型, 实体 id) 列表。"""
    refs: list[tuple[str, str]] = []
    for emit in spec.emits:
        for value in _resolve(result, emit.path):
            if isinstance(value, str) and value:
                refs.append((emit.entity_type, value))
    return refs


async def register_result(
    store: ProvenanceStore,
    session_id: str,
    spec: ToolSpec,
    result: dict[str, Any],
) -> None:
    """执行成功后登记本次调用产出的实体。"""
    refs = emitted_entity_ids(spec, result)
    if refs:
        await store.register(session_id, refs)


async def check_requirements(
    store: ProvenanceStore,
    session_id: str,
    spec: ToolSpec,
    args: dict[str, Any],
) -> str | None:
    """校验写操作引用的实体是否都已在本会话出现过。返回拒绝原因或 None。

    参数缺失或类型不对时这里一律跳过：那些情况由判定链第 4 步的参数契约报错，
    那条消息会点名工具与参数，对 Agent 更可读。这里重复报一次只会让拒绝原因
    变长，而「参数畸形」与「实体没读过」本来就是两件不同的事。
    """
    for require in spec.requires:
        value = args.get(require.arg)
        if not isinstance(value, str) or not value:
            continue
        if await store.contains(session_id, require.entity_type, value):
            continue
        return (
            f"Provenance 拒绝：实体 {require.entity_type}:{value!r} 未在会话 {session_id} "
            f"中出现过。写操作只能作用于本会话工具返回过的实体。"
        )
    return None
