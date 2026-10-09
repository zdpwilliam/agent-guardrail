"""MCP 适配器（spec §19.3）：把注册的工具以 MCP 协议暴露给外部客户端。

实现 stdio 上的 JSON-RPC 2.0（MCP 基础子集）：initialize / tools/list /
tools/call。外部 MCP 客户端零改动接入——所有调用走进程内网关 ASGI，
护栏语义与 HTTP 路径完全一致。

外部 Agent 需自带 session_id（或在 initialize 里建会话）。这是刻意的：
护栏不做身份发明，会话归属由调用方声明。认证启用时，客户端通过
`GUARDRAIL_API_KEY` 环境变量提供 agent API key。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

_PROTOCOL_VERSION = "2024-11-05"


def _tool_descriptions() -> list[dict]:
    from guardrail.tools.registry import TOOL_SPECS

    return [
        {
            "name": spec.name,
            "description": f"{spec.kind} 工具（经会话风险护栏）",
            "inputSchema": {
                **spec.args_schema,
                "required": [*spec.args_schema.get("properties", {}), "session_id"],
                "properties": {
                    **spec.args_schema.get("properties", {}),
                    "session_id": {"type": "string",
                                   "description": "护栏会话 id"},
                },
            },
        }
        for spec in TOOL_SPECS.values()
    ]


def _result_text(resp: httpx.Response) -> list[dict]:
    return [{"type": "text", "text": json.dumps(resp.json(), ensure_ascii=False)}]


async def handle_mcp_message(
    app: Any,  # noqa: ANN401 - FastAPI app
    msg: dict,
    api_key: str | None = None,
) -> dict | None:
    """处理一条 MCP JSON-RPC 消息，返回响应（notification 返回 None）。"""
    method = msg.get("method", "")
    mid = msg.get("id")
    params = msg.get("params", {}) or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "agent-guardrail", "version": "1.0.0"},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _tool_descriptions()}}
    if method == "tools/call":
        name = params.get("name", "")
        args = dict(params.get("arguments", {}))
        session_id = args.pop("session_id", params.get("session_id", ""))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mcp"
        ) as c:
            resp = await c.post(f"/v1/tools/{name}",
                                json={"session_id": session_id, "args": args},
                                headers={"X-API-Key": api_key} if api_key else None)
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "content": _result_text(resp), "isError": resp.status_code >= 400,
        }}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"未知方法: {method}"}}
    return None


async def serve_stdio(db_path: str = "data/gateway.db") -> int:
    """stdio 主循环。构建完整 demo 栈（商城进程内），护栏语义与 HTTP 一致。"""
    from guardrail.demo import build_demo_stack

    async with build_demo_stack(db_path) as (app, _client):
        api_key = os.getenv("GUARDRAIL_API_KEY")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                resp = {"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "解析失败"}}
            else:
                resp = await handle_mcp_message(app, msg, api_key=api_key)
            if resp is not None:
                sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    return 0
