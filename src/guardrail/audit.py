from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

# 链的首条记录的 prev_hash。它让「第一条」和「第 N 条」走同一条代码路径。
GENESIS_HASH = "0" * 64
REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = {
    "email",
    "to",
    "body",
    "content",
    "phone",
    "address",
    "beneficiary_account",
    "password",
    "secret",
    "token",
    "api_key",
    "authorization",
    "card_number",
    "cvv",
    "iban",
    "account_number",
}
_SENSITIVE_SUFFIXES = (
    "_email",
    "_phone",
    "_address",
    "_account",
    "_password",
    "_secret",
    "_token",
    "_api_key",
    "_authorization",
)
_SENSITIVE_MARKERS = (
    "email",
    "phone",
    "address",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "beneficiary",
    "card_number",
    "cvv",
    "iban",
)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_").strip()
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith(_SENSITIVE_SUFFIXES)
        or any(marker in normalized for marker in _SENSITIVE_MARKERS)
    )


def redact_sensitive(value: Any) -> Any:  # noqa: ANN401 - 递归处理任意 JSON 值
    """递归脱敏审计参数中的常见 PII / 凭证字段。

    脱敏发生在写审计存储之前，并且脱敏后的值参与哈希计算；因此审计链仍然
    可以证明「记录没有被改动」，同时不会把原始邮箱、手机号、正文或账户号
    留在 SQLite 文件里。原始参数只存在于当次调用的内存和计划/审批业务表中。
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_sensitive_key(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value

Decision = Literal["allow", "allow_with_flag", "ask", "deny"]


def canonical_json(value: Any) -> str:  # noqa: ANN401 - 序列化任意 JSON 值
    """稳定的 JSON 序列化。

    sort_keys 保证键序无关；separators 去掉空白；ensure_ascii=False 让中文留在
    链上可读——转成 \\uXXXX 同样是确定性的，但审计链的主要读者是人。

    这个函数被幂等键和审计链共用，所以它一旦改动，两边的历史数据会同时失效。
    真要改，必须配一次数据迁移；不要为了「统一风格」动它。
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_entry_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return sha256_hex(prev_hash + canonical_json(payload))


class AuditDraft(BaseModel):
    """一条待追加的审计记录（还没有链的位置信息）。"""

    session_id: str
    plan_id: str | None = None
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    decision: Decision
    # replay 单独成字段而不是塞进 decision：decision 是策略判定，会被
    # /metrics 按值计数（spec §19.4），掺进一个非判定值会污染那个指标。
    # spec §6.2 原文写的是 `decision: replay`，此处有意偏离，理由同上。
    replay: bool = False
    reasons: list[str] = Field(default_factory=list)
    timestamp: str


class AuditEntry(AuditDraft):
    """链上的一条记录。"""

    seq: int
    prev_hash: str
    entry_hash: str

    def hashed_payload(self) -> dict[str, Any]:
        """参与哈希的字段。

        刻意不含 seq / prev_hash / entry_hash 自身：prev_hash 已经以
        「前缀拼接」的方式参与了计算（spec §7），再放进 payload 会重复计入。
        """
        return {
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "tool": self.tool,
            "args": self.args,
            "decision": self.decision,
            "replay": self.replay,
            "reasons": self.reasons,
            "timestamp": self.timestamp,
        }


class ChainVerdict(BaseModel):
    ok: bool
    checked: int
    broken_at_seq: int | None = None
    reason: str | None = None


class AuditSink(Protocol):
    """审计后端（spec §19.1 的第二个接缝）。

    审计是唯一必须比业务更持久的东西，所以它是独立接缝而不是业务表的一个
    视图。当前实现是 SQLite 哈希链；预留实现是 WORM 存储 / 外部日志。
    """

    async def append(self, draft: AuditDraft) -> AuditEntry: ...

    async def verify_chain(self, since_seq: int = 0) -> ChainVerdict: ...

    async def head_hash(self) -> str: ...

    async def list_entries(self, session_id: str, limit: int = 200) -> list[AuditEntry]: ...

    async def head(self) -> tuple[int, str]: ...

    async def verify_anchor(self, seq: int, entry_hash: str) -> ChainVerdict: ...
