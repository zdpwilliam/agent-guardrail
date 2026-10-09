from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from guardrail.models import SessionState


class SessionRecord(BaseModel):
    session_id: str
    agent_id: str
    task_id: str | None = None
    created_at: str
    # 会话有效期（spec §3.1，默认 30 分钟）。判定链第 1 步读它：
    # 一个过期的会话必须被拒绝，否则「组合风险」的载体在时间上就是漏的。
    expires_at: str


class SessionStore(Protocol):
    """会话存储协议。

    当前唯一实现是 SQLite（单进程）。多进程扩展只需新增一个实现——
    调用方代码不变。见 spec §19.1 / §19.2。

    身份（SessionRecord）与风险状态（SessionState）刻意分开存取：
    身份几乎只写一次，状态每次调用都读写——混在一批只会让 CAS 的粒度变粗。
    """

    async def load(self, session_id: str) -> SessionRecord | None: ...

    async def save(self, record: SessionRecord) -> None: ...

    async def sweep_expired(self, now_iso: str) -> int:
        """删除已过期会话，返回删除条数。"""
        ...

    async def load_state(self, session_id: str) -> tuple[SessionState, int] | None:
        """返回 (风险状态, 版本号)。会话存在但从未写过状态时返回 (全新状态, 0)。
        会话不存在返回 None。"""
        ...

    async def save_state(self, session_id: str, state: SessionState,
                         expected_version: int) -> bool:
        """CAS 写入：版本失配返回 False，调用方必须重载重算（spec §3.7.3），
        绝不允许「尽力而为地覆盖」。"""
        ...

    async def find_by_task(
        self, task_id: str
    ) -> list[tuple[SessionRecord, SessionState]]:
        """跨 Agent 合并的唯一入口（spec §19.1）：同一 task_id 下全部会话的
        (身份, 状态) 快照。contributors 过滤需要 agent_id，而 SessionState
        刻意不携带身份字段。"""
        ...


class DecisionEvent(BaseModel):
    session_id: str
    tool: str
    decision: str
    reasons: list[str]
    timestamp: str


class DecisionEventBus(Protocol):
    """决策事件总线（spec §19.1 的第三个接缝）。

    控制台（M5）需要看到待审批项；单进程下同步调用就够。保留协议是为了将来
    换 Redis Pub/Sub 时调用方代码一行不改。
    """

    def publish(self, event: DecisionEvent) -> None: ...


class PendingApproval(BaseModel):
    """一张待审批单（spec §10.1 的 ask 产物，§15 的 pending_approvals 表）。

    cost 在 authorize 时算好存进来：approve 后执行时按**当时**的成本扣，
    不重算——策略文件中途改动不应追溯影响已扣下的调用。
    """

    id: str
    session_id: str
    agent_id: str
    tool: str
    args: dict
    reasons: list[str]
    cost: float = 0.0
    created_at: str
    resolved_at: str | None = None
    resolution: str | None = None  # approve | reject
    decided_by: str | None = None
    comment: str | None = None


class ApprovalStore(Protocol):
    async def create(self, approval: PendingApproval) -> PendingApproval: ...

    async def load(self, approval_id: str) -> PendingApproval | None: ...

    async def resolve(self, approval_id: str, resolution: str,
                      decided_by: str, comment: str | None = None
                      ) -> PendingApproval | None:
        """原子认领：UPDATE ... WHERE resolved_at IS NULL。返回 None = 已处理或不存在。"""
        ...

    async def list_open(self, limit: int = 50) -> list[PendingApproval]: ...
