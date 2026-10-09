"""M9 可观测性 + M10 适配器测试。"""


import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.config import Settings
from guardrail.langchain_tools import get_guardrail_tools
from guardrail.main import create_app
from guardrail.mcp_server import handle_mcp_message
from shop.main import create_app as create_shop_app


@pytest.fixture
async def stack(tmp_path):
    shop_app = create_shop_app(str(tmp_path / "shop.db"))
    async with LifespanManager(shop_app):
        app = create_app(
            Settings(shop_base_url="http://shop.test",
                     gateway_db_path=str(tmp_path / "gateway.db")),
            shop_transport=httpx.ASGITransport(app=shop_app),
        )
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                yield app, c


# ---------- M9：/readyz 与 /metrics ----------


async def test_readyz_ok(stack):
    _, c = stack
    r = await c.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_metrics_reflect_traffic(stack):
    app, c = stack
    sid = (await c.post("/v1/sessions", json={"agent_id": "ops_agent",
                                              "task_id": "t-1"})).json()["session_id"]
    await c.post("/v1/tools/list_products", json={"session_id": sid, "args": {}})
    r = await c.post("/v1/tools/update_price", json={
        "session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -90.0}})
    assert r.status_code == 403
    m = await c.get("/metrics")
    assert m.status_code == 200
    assert 'guardrail_audit_entries_total{decision="deny"}' in m.text
    assert 'guardrail_audit_entries_total{decision="allow"}' in m.text
    assert "单次降价" in m.text  # 拒绝原因可回答「这次为什么被拒」


# ---------- M10：MCP 适配器 ----------


async def test_mcp_initialize_and_tools_list(stack):
    app, _ = stack
    r = await handle_mcp_message(app, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r["result"]["serverInfo"]["name"] == "agent-guardrail"
    r2 = await handle_mcp_message(app, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in r2["result"]["tools"]]
    assert set(names) == {"update_price", "update_stock", "create_coupon", "create_order",
                          "refund_order", "send_email", "list_products",
                          "get_product", "get_order",
                          "list_files", "read_file", "write_file",
                          "read_forum", "post_forum", "create_ticket", "execute_wire"}


async def test_mcp_call_goes_through_guardrail(stack):
    app, _ = stack
    sid = (await (await _http(stack)).post("/v1/sessions", json={
        "agent_id": "ops_agent", "task_id": "t-1"})).json()["session_id"]
    await handle_mcp_message(app, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                   "params": {"name": "list_products",
                                              "arguments": {"session_id": sid}}})
    # 试图打一折：护栏语义经 MCP 路径依然生效。
    r = await handle_mcp_message(app, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                       "params": {"name": "update_price",
                                                  "arguments": {"session_id": sid,
                                                                "product_id": "p-iphone",
                                                                "delta_pct": -90.0}}})
    assert r["result"]["isError"] is True
    assert "单次降价" in r["result"]["content"][0]["text"]


async def test_mcp_call_passes_agent_api_key_when_auth_enabled(stack):
    app, c = stack
    app.state.settings.agent_api_keys = {"ops-key": "ops_agent"}
    app.state.settings.approver_api_keys = {"approver-key": "boss"}
    sid = (await c.post(
        "/v1/sessions",
        headers={"X-API-Key": "ops-key"},
        json={"agent_id": "ops_agent", "task_id": "t-auth"},
    )).json()["session_id"]

    r = await handle_mcp_message(
        app,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "list_products",
                "arguments": {"session_id": sid},
            },
        },
        api_key="ops-key",
    )
    assert r["result"]["isError"] is False


async def test_mcp_unknown_method(stack):
    app, _ = stack
    r = await handle_mcp_message(app, {"jsonrpc": "2.0", "id": 9, "method": "wat"})
    assert r["error"]["code"] == -32601


# ---------- M10：LangChain 适配器 ----------


async def test_langchain_tools_duck_typing(stack):
    _, c = stack
    sid = (await c.post("/v1/sessions", json={"agent_id": "pricing_agent",
                                              "task_id": "t-1"})).json()["session_id"]
    tools = get_guardrail_tools(session_id=sid, gateway="http://t",
                                transport=httpx.ASGITransport(app=stack[0]))
    assert len(tools) == 16
    tp = next(t for t in tools if t.name == "update_price")
    assert "update_price" in tp.description
    assert "product_id" in tp.args_schema["properties"]
    # provenance 门：写前必须先读（经护栏路径读，同会话）。
    prime = next(t for t in tools if t.name == "list_products")
    prime._run()
    read = next(t for t in tools if t.name == "get_product")
    read._run(product_id="p-iphone")
    out = tp._run(product_id="p-iphone", delta_pct=-3.0)
    assert out["decision"] == "allow"


async def test_langchain_tool_respects_guardrail(stack):
    _, c = stack
    sid = (await c.post("/v1/sessions", json={"agent_id": "pricing_agent",
                                              "task_id": "t-1"})).json()["session_id"]
    tools = get_guardrail_tools(session_id=sid, gateway="http://t",
                                transport=httpx.ASGITransport(app=stack[0]))
    read = next(t for t in tools if t.name == "get_product")
    read._run(product_id="p-iphone")
    tp = next(t for t in tools if t.name == "update_price")
    out = tp._run(product_id="p-iphone", delta_pct=-50.0)
    assert "单次降价" in out["detail"]


async def test_langchain_tool_passes_agent_api_key_when_auth_enabled(stack):
    app, c = stack
    app.state.settings.agent_api_keys = {"ops-key": "ops_agent"}
    app.state.settings.approver_api_keys = {"approver-key": "boss"}
    sid = (await c.post(
        "/v1/sessions",
        headers={"X-API-Key": "ops-key"},
        json={"agent_id": "ops_agent", "task_id": "t-auth"},
    )).json()["session_id"]
    tools = get_guardrail_tools(
        session_id=sid,
        gateway="http://t",
        transport=httpx.ASGITransport(app=app),
        api_key="ops-key",
    )
    out = next(t for t in tools if t.name == "list_products")._run()
    assert out["decision"] == "allow"


async def _http(stack):
    _, c = stack
    return c
