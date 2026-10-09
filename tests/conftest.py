from contextlib import asynccontextmanager

import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.config import Settings


@asynccontextmanager
async def app_client(app):
    """把一个 ASGI app 包成 httpx.AsyncClient，并正确触发 lifespan。

    httpx 的 ASGITransport **不会**跑 lifespan 事件，所以必须用 LifespanManager
    包一层。否则 app.state.store 等不会被初始化，所有依赖它的测试都会失败。
    """
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


@pytest.fixture
def settings(tmp_path):
    return Settings(
        shop_base_url="http://testserver",
        gateway_db_path=str(tmp_path / "gateway.db"),
    )
