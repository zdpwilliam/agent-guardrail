from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from guardrail.audit import canonical_json, sha256_hex


def idempotency_key(session_id: str, tool: str, args: dict[str, Any]) -> str:
    """幂等键 = sha256(canonical_json({session_id, tool, args}))。

    spec §6.2 写的是 `sha256(session_id ‖ tool ‖ canonical_json(args))`。这里改成
    整体规范化之后哈希：字段直接拼接会碰撞——("ab", "c") 与 ("a", "bc") 拼出
    同一个字符串，于是两个不同的调用共用一个幂等键，后者的响应会被原样返回给
    前者。语义完全一致，消掉了分隔符歧义。

    `args` 必须是 **Agent 提交的原始参数**，不能是服务端派生之后的版本：
    派生结果依赖当时的商城状态，同一个意图在两次调用里会算出不同的
    absolute_delta_cents，键就永远对不上。
    """
    return sha256_hex(canonical_json({"session_id": session_id, "tool": tool, "args": args}))


class IdempotencyRecord(BaseModel):
    key: str
    session_id: str
    status: str  # in_progress | done
    response: dict[str, Any] | None = None
    created_at: str


class IdempotencyStore(Protocol):
    """幂等登记（spec §6.2）。

    幂等不是可选的健壮性装饰：Agent 会在超时、重试、网络抖动时重复发出同一个
    调用。没有幂等，一次「降价 5%」的重试就变成「降价 10%」——而这恰好绕过
    单次阈值，因为策略看到的是两个各自合规的 5%。幂等是单次阈值能成立的前提。
    """

    async def get(self, key: str) -> IdempotencyRecord | None: ...

    async def begin(self, key: str, session_id: str) -> bool:
        """登记一次进行中的调用。已被占用时返回 False（调用方应回 409）。"""
        ...

    async def complete(self, key: str, response: dict[str, Any]) -> None: ...

    async def release(self, key: str) -> None:
        """执行失败时释放登记，让 Agent 能真正重试。"""
        ...
