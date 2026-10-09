"""P1:控制台鉴权(GUARDRAIL_CONSOLE_KEY)。设置后 /console* 必须携带
X-Console-Key;未设置保持开发模式全开。/metrics /readyz 不受限。
"""

import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.config import Settings
from guardrail.main import create_app
from shop.main import create_app as create_shop_app


@pytest.fixture
async def console_client(tmp_path):
    """工厂 fixture:yield 一个「async with 进上下文拿 client」的工厂。"""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def make_client(**overrides):
        key = overrides.get("console_key")
        shop_app = create_shop_app(
            str(tmp_path / f"shop-{key or 'dev'}.db"))
        async with LifespanManager(shop_app):
            app = create_app(
                Settings(shop_base_url="http://shop.test",
                         gateway_db_path=str(tmp_path / f"gw-{key or 'dev'}.db"),
                         **overrides),
                shop_transport=httpx.ASGITransport(app=shop_app),
            )
            async with LifespanManager(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport,
                                             base_url="https://t") as c:
                    yield c

    return make_client


async def test_console_key_set_blocks_without_header(console_client):
    async with console_client(console_key="s3cret") as c:
        r = await c.get("/console")
        assert r.status_code == 401


async def test_console_key_set_accepts_correct_header(console_client):
    async with console_client(console_key="s3cret") as c:
        r = await c.get("/console", headers={"X-Console-Key": "s3cret"})
        assert r.status_code == 200


async def test_console_key_unset_keeps_open_dev_mode(console_client):
    """未设置 key:开发模式全开(既有测试基线不变)。"""
    async with console_client() as c:
        assert (await c.get("/console")).status_code == 200


async def test_metrics_and_readyz_not_gated(console_client):
    """探针端点不鉴权——K8s liveness/readiness 依赖它们。"""
    async with console_client(console_key="s3cret") as c:
        assert (await c.get("/readyz")).status_code == 200
        assert (await c.get("/metrics")).status_code == 200


async def test_console_api_actions_also_gated(console_client):
    """审批/播放 POST 同样受保护——只拦页面不拦动作等于没拦。"""
    async with console_client(console_key="s3cret") as c:
        r = await c.post("/console/demo/run")
        assert r.status_code == 401


async def test_console_cookie_login_allows_browser_access(console_client):
    async with console_client(console_key="s3cret") as c:
        denied = await c.get("/console")
        assert denied.status_code == 401

        login_page = await c.get("/console/login")
        assert login_page.status_code == 200
        csrf = c.cookies["guardrail_csrf"]
        assert csrf in login_page.text

        login = await c.post(
            "/console/login",
            data={"console_key": "s3cret", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert "guardrail_console_key" in c.cookies

        allowed = await c.get("/console")
        assert allowed.status_code == 200


async def test_console_cookie_login_rejects_wrong_key(console_client):
    async with console_client(console_key="s3cret") as c:
        await c.get("/console/login")
        login = await c.post(
            "/console/login",
            data={
                "console_key": "wrong",
                "csrf_token": c.cookies["guardrail_csrf"],
            },
            follow_redirects=False,
        )
        assert login.status_code == 401


async def test_console_post_requires_csrf_token(console_client):
    async with console_client(console_key="s3cret") as c:
        await c.get("/console/login")
        denied = await c.post(
            "/console/login",
            data={"console_key": "s3cret"},
            follow_redirects=False,
        )
        assert denied.status_code == 403


async def test_authenticated_console_action_requires_csrf_token(console_client):
    async with console_client(console_key="s3cret") as c:
        denied = await c.post(
            "/console/demo/run",
            headers={"X-Console-Key": "s3cret"},
            follow_redirects=False,
        )
        assert denied.status_code == 403
