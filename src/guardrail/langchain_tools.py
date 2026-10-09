"""LangChain 适配器：把护栏后的注册工具暴露为 LangChain 风格的工具对象。

刻意**不 import langchain**：适配层只依赖 duck-typed 约定
（name / description / args_schema / _run），LangChain 的 BaseTool 可以直接
包这个对象（或用 @tool 装饰器包装 _run）。避免硬依赖的意义：接入方版本
碎片化严重，护栏的依赖树应该为零。

用法（接入方代码）：
    tools = get_guardrail_tools(
        session_id="s-1", gateway="http://127.0.0.1:8000", api_key="agent-key"
    )
    llm_with_tools = llm.bind_tools([wrap_with_langchain(t) for t in tools])
"""

from __future__ import annotations

from typing import Any

import httpx

from guardrail.tools.registry import TOOL_SPECS


class GuardrailTool:
    """护栏工具对象（duck-typed BaseTool）。

    _run 同步阻塞调用网关 HTTP——LangChain 的工具执行模型是同步为主，
    异步由接入方的 executor 处理。
    """

    def __init__(self, spec: Any,  # noqa: ANN401 - duck-typed ToolSpec
                 session_id: str, gateway: str,
                 transport: httpx.AsyncTransport | None = None,
                 api_key: str | None = None) -> None:
        self._spec = spec
        self._session_id = session_id
        self._gateway = gateway.rstrip("/")
        self._transport = transport
        self._api_key = api_key

    @property
    def name(self) -> str:
        return self._spec.name

    @property
    def description(self) -> str:
        return f"[经会话风险护栏] {self._spec.kind} 工具：{self._spec.name}"

    @property
    def args_schema(self) -> dict:
        return self._spec.args_schema

    def _run(self, **kwargs: Any) -> dict:  # noqa: ANN401 - LangChain 透传任意参数
        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=self._transport, base_url=self._gateway
            ) as c:
                return await c.post(f"/v1/tools/{self.name}", json={
                    "session_id": self._session_id, "args": kwargs},
                    headers={"X-API-Key": self._api_key} if self._api_key else None)

        resp = asyncio_run(_call())
        return resp.json()

    def __repr__(self) -> str:
        return f"GuardrailTool({self.name!r})"


def asyncio_run(coro: Any) -> Any:  # noqa: ANN401 - 事件循环适配
    """接入方大多在同步上下文里调 _run；已有事件循环时复用它。"""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def get_guardrail_tools(session_id: str, gateway: str,
                        transport: httpx.AsyncTransport | None = None,
                        api_key: str | None = None) -> list[GuardrailTool]:
    """全部护栏工具。session_id 由接入方先经 POST /v1/sessions 创建。"""
    return [GuardrailTool(spec, session_id, gateway, transport, api_key)
            for spec in TOOL_SPECS.values()]
