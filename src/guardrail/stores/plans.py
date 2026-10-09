from __future__ import annotations

import json

from guardrail.clock import now_iso
from guardrail.plans import Plan, is_legal_transition
from guardrail.stores.sqlite import SqliteBackend


class SqlitePlanStore:
    """plans 表存储。

    消费记账必须 CAS：token 无状态、消费有状态——同一计划两个并发步骤同时
    记账必须恰一成功，输的一方重载最新已执行列表再追加自己的哈希（§4.5
    第 2 条的记账面）。
    """

    def __init__(self, backend: SqliteBackend) -> None:
        self.backend = backend

    async def create(self, plan: Plan) -> Plan:
        await self.backend.execute(
            "INSERT INTO plans"
            " (plan_id, session_id, agent_id, intent, actions_json, status,"
            "  risk_level, triggered_rules_json, reasons_json, projected_json,"
            "  token_json, token_expires_at, executed_hashes_json, outputs_json,"
            "  created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan.plan_id, plan.session_id, plan.agent_id, plan.intent,
                json.dumps([a.model_dump() for a in plan.actions], ensure_ascii=False),
                plan.status, plan.risk_level,
                json.dumps(plan.triggered_rules, ensure_ascii=False),
                json.dumps(plan.reasons, ensure_ascii=False),
                plan.projected_json, plan.token_json, plan.token_expires_at,
                json.dumps(plan.executed_hashes),
                json.dumps(plan.outputs), plan.created_at,
            ),
        )
        await self.backend.commit()
        return plan

    async def load(self, plan_id: str) -> Plan | None:
        row = await self.backend.fetchone("SELECT * FROM plans WHERE plan_id = ?", (plan_id,))
        return _to_plan(row) if row is not None else None

    async def load_with_version(self, plan_id: str) -> tuple[Plan, int] | None:
        row = await self.backend.fetchone(
            "SELECT *, version FROM plans WHERE plan_id = ?", (plan_id,)
        )
        if row is None:
            return None
        return _to_plan(row), row["version"]

    async def list_by_session(self, session_id: str, limit: int = 50) -> list[Plan]:
        """控制台会话页的计划卡片数据源。"""
        rows = await self.backend.fetchall(
            "SELECT * FROM plans WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        )
        return [_to_plan(r) for r in rows]

    async def list_by_status(self, status: str, limit: int = 50) -> list[Plan]:
        rows = await self.backend.fetchall(
            "SELECT * FROM plans WHERE status = ? ORDER BY created_at ASC LIMIT ?",
            (status, limit),
        )
        return [_to_plan(r) for r in rows]

    async def update_status(
        self, plan_id: str, target: str, *,
        decided_by: str | None = None,
        token_json: str | None = None,
        token_expires_at: str | None = None,
    ) -> bool:
        """状态迁移。非法迁移直接抛错——状态机是设计的一部分，静默拒绝会掩盖调用方 bug。"""
        plan = await self.load(plan_id)
        if plan is None:
            return False
        if not is_legal_transition(plan.status, target):
            raise ValueError(
                f"计划状态迁移非法：{plan.status} → {target}（spec §4.6 生命周期）"
            )
        cur = await self.backend.execute(
            "UPDATE plans SET status = ?, decided_at = ?,"
            " decided_by = COALESCE(?, decided_by),"
            " token_json = COALESCE(?, token_json),"
            " token_expires_at = COALESCE(?, token_expires_at)"
            " WHERE plan_id = ? AND status = ?",
            (target, now_iso(), decided_by, token_json, token_expires_at,
             plan_id, plan.status),
        )
        await self.backend.commit()
        return (cur.rowcount or 0) > 0

    async def consume_hash(
        self, plan_id: str, action_hash: str, expected_version: int,
        outputs: dict[str, str] | None = None,
    ) -> bool:
        """CAS 追加已消费哈希 + 记录本步真实产出（供后续步骤占位符解析）。
        失配返回 False，调用方重载重试。"""
        plan, version = await self._load_with_version(plan_id)
        if plan is None or version != expected_version:
            return False
        merged_outputs = {**plan.outputs, **(outputs or {})}
        cur = await self.backend.execute(
            "UPDATE plans SET executed_hashes_json = ?, outputs_json = ?,"
            " version = version + 1"
            " WHERE plan_id = ? AND version = ?",
            (json.dumps([*plan.executed_hashes, action_hash]),
             json.dumps(merged_outputs), plan_id, expected_version),
        )
        await self.backend.commit()
        return (cur.rowcount or 0) > 0

    async def _load_with_version(self, plan_id: str) -> tuple[Plan | None, int]:
        row = await self.backend.fetchone(
            "SELECT *, version FROM plans WHERE plan_id = ?", (plan_id,)
        )
        if row is None:
            return None, -1
        return _to_plan(row), row["version"]

    async def expire_if_due(self, plan_id: str, now: str | None = None) -> bool:
        """惰性过期：approved 且 token 已到期的计划置 expired（§4.6）。

        pending 计划不在此列——没人批过就没有 TTL 一说；审批接口在读它时
        自会检查。"""
        plan = await self.load(plan_id)
        if plan is None or plan.status != "approved":
            return False
        if plan.token_expires_at is None:
            return False
        from guardrail.plans import timestamp_expired

        # 只看 token_expires_at，不要求 token_json 仍可解析——过期是时间事实。
        if not timestamp_expired(plan.token_expires_at, now or now_iso()):
            return False
        return await self.update_status(plan_id, "expired")


def _to_plan(row) -> Plan:  # noqa: ANN001 - aiosqlite.Row
    return Plan(
        plan_id=row["plan_id"],
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        intent=row["intent"],
        actions=[json.loads(a) if isinstance(a, str) else a
                 for a in json.loads(row["actions_json"])],
        status=row["status"],
        risk_level=row["risk_level"],
        triggered_rules=json.loads(row["triggered_rules_json"]),
        reasons=json.loads(row["reasons_json"]),
        projected_json=row["projected_json"],
        token_json=row["token_json"],
        token_expires_at=row["token_expires_at"],
        executed_hashes=json.loads(row["executed_hashes_json"]),
        outputs=json.loads(row["outputs_json"]),
        created_at=row["created_at"],
        decided_at=row["decided_at"],
        decided_by=row["decided_by"],
    )
