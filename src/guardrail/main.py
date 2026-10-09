from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sys
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.responses import JSONResponse

from guardrail.api import approvals, audit, console, obs, plans_api, sessions, tools
from guardrail.api.auth import validate_auth_settings
from guardrail.config import Settings, get_settings
from guardrail.domains.corp import CorpState
from guardrail.plans import submit_plan
from guardrail.policy.loader import (
    load_combined_policy,
    load_plan_policy,
    load_policy,
)
from guardrail.policy.single import PolicyError
from guardrail.stores.approvals import SqliteApprovalStore
from guardrail.stores.audit import SqliteAuditSink
from guardrail.stores.idempotency import SqliteIdempotencyStore
from guardrail.stores.plans import SqlitePlanStore
from guardrail.stores.provenance import SqliteProvenanceStore
from guardrail.stores.sqlite import SqliteBackend, SqliteSessionStore
from guardrail.tools.registry import assert_specs_valid


class InProcessDecisionEventBus:
    """进程内同步事件总线（spec §19.1）。

    控制台（M5）需要看到待审批项；单进程下同步调用就够。保留协议是为了将来
    换 Redis Pub/Sub 时调用方代码一行不改。
    """

    def __init__(self) -> None:
        self._subscribers: list[Any] = []

    def subscribe(self, handler: Any) -> None:  # noqa: ANN401
        self._subscribers.append(handler)

    def publish(self, event: Any) -> None:  # noqa: ANN401
        for handler in list(self._subscribers):
            handler(event)


_log = logging.getLogger("guardrail.access")


class RequestBodyTooLargeError(Exception):
    """请求体在流式读取阶段越过配置上限。"""


def _configure_json_access_logging(logger: logging.Logger) -> None:
    """给访问日志挂一个只写 message 的 stdout handler。

    `logger.info(json.dumps(...))` 只保证 message 本身是 JSON；若由 root/default
    formatter 输出，行首会多出时间、level 或 logger 名称，容器日志采集器就不能
    按 JSON 行解析。生产环境使用专用 formatter，并关闭 propagation 防止重复。
    """
    marker = "_guardrail_json_handler"
    if not any(getattr(handler, marker, False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        setattr(handler, marker, True)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


async def _buffer_request_body(request: Request, max_bytes: int) -> None:
    """把实际请求体读入缓存并执行上限校验。

    只看 `Content-Length` 会漏掉 chunked 请求，或者在 header 与实际 body 不一致
    时误放行。这里直接消费 ASGI stream，读过上限立即停止，然后把已验证的 body
    放回 Request 缓存，后续 Pydantic/Form 解析仍走正常路径。
    """
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise RequestBodyTooLargeError
    body_bytes = bytes(body)
    request._body = body_bytes  # noqa: SLF001 - Starlette 的公开缓存约定

    async def replay_body() -> dict[str, Any]:
        # BaseHTTPMiddleware 每一层会构造新的 Request；只缓存 _body 不足以让
        # 内层中间件和端点看到请求体。把已验证的 body 作为可重放 receive 传下去。
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    request._receive = replay_body  # noqa: SLF001


def _set_csrf_cookie(response: Any, token: str, *, secure: bool) -> None:  # noqa: ANN401
    response.set_cookie(
        console.CSRF_COOKIE,
        token,
        httponly=False,
        secure=secure,
        samesite="strict",
        max_age=8 * 60 * 60,
    )


def _apply_security_headers(response: Any, *, production: bool) -> None:  # noqa: ANN401
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'"
    )
    if production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


def create_app(
    settings: Settings | None = None,
    shop_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """shop_transport 仅用于测试：把商城 app 挂在内存里，免起真实进程。"""
    resolved = settings or get_settings()
    validate_auth_settings(resolved)
    if resolved.environment == "production":
        _configure_json_access_logging(_log)
    rate_lock = asyncio.Lock()
    rate_buckets: dict[str, tuple[float, int]] = {}

    # 策略在 app 构造期加载，不在 lifespan 里：一个没加载上策略的网关应当在
    # 起进程时就死掉，而不是变成一个「看起来在跑、其实不拦任何东西」的服务。
    # 两份策略 fail-closed 一视同仁（spec §10.2）。
    try:
        policy = load_policy(resolved.policy_path)
        combined_policy = load_combined_policy(resolved.combined_policy_path)
        plan_policy = load_plan_policy(resolved.plan_policy_path)
    except PolicyError as exc:
        raise RuntimeError(f"策略加载失败，拒绝启动：{exc}") from exc
    assert_specs_valid()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        backend = SqliteBackend(
            resolved.gateway_db_path,
            busy_timeout_ms=resolved.db_busy_timeout_ms,
        )
        await backend.connect()
        app.state.backend = backend
        app.state.sessions = SqliteSessionStore(backend)
        app.state.audit = SqliteAuditSink(backend)
        app.state.idempotency = SqliteIdempotencyStore(backend)
        app.state.provenance = SqliteProvenanceStore(backend)
        app.state.approvals = SqliteApprovalStore(backend)
        app.state.bus = InProcessDecisionEventBus()
        app.state.policy = policy
        app.state.combined_policy = combined_policy
        app.state.plan_policy = plan_policy
        app.state.corp = CorpState()
        app.state.settings = resolved
        app.state.plans = SqlitePlanStore(backend)
        app.state.submit_plan = submit_plan
        app.state.shop = httpx.AsyncClient(
            base_url=resolved.shop_base_url, transport=shop_transport
        )
        yield
        await app.state.shop.aclose()
        await backend.close()

    app = FastAPI(title="会话级风险护栏", lifespan=lifespan)
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).resolve().parent / "static"),
        name="static",
    )
    app.include_router(sessions.router)
    app.include_router(tools.router)
    app.include_router(audit.router)
    app.include_router(plans_api.router)
    app.include_router(console.router)

    def rate_identity(request: Request) -> str:
        key = request.headers.get("x-api-key")
        if not key:
            authorization = request.headers.get("authorization", "")
            if authorization.lower().startswith("bearer "):
                key = authorization[7:].strip()
        if key:
            return f"key:{key}"
        host = request.client.host if request.client is not None else "unknown"
        return f"ip:{host}"

    @app.middleware("http")
    async def production_guard(
        request: Request,
        call_next: Any,  # noqa: ANN401 - starlette middleware 签名
    ) -> Any:  # noqa: ANN401 - 返回 Response
        """请求体上限、限流和 JSON 访问日志。"""
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        t0 = time.perf_counter()
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                response = JSONResponse(
                    {"detail": "Content-Length 非法"}, status_code=400
                )
                response.headers["X-Request-ID"] = request_id
                return response
            if declared_length < 0:
                response = JSONResponse(
                    {"detail": "Content-Length 非法"}, status_code=400
                )
                response.headers["X-Request-ID"] = request_id
                return response
            if declared_length > resolved.max_request_bytes:
                response = JSONResponse(
                    {"detail": "请求体超过大小限制"}, status_code=413
                )
                response.headers["X-Request-ID"] = request_id
                return response

        if request.method not in ("GET", "HEAD", "OPTIONS"):
            try:
                await _buffer_request_body(request, resolved.max_request_bytes)
            except RequestBodyTooLargeError:
                response = JSONResponse(
                    {"detail": "请求体超过大小限制"}, status_code=413
                )
                response.headers["X-Request-ID"] = request_id
                return response

        if request.url.path not in ("/healthz", "/readyz", "/metrics"):
            identity = rate_identity(request)
            now = time.monotonic()
            async with rate_lock:
                window_start, count = rate_buckets.get(identity, (now, 0))
                if now - window_start >= resolved.rate_limit_window_seconds:
                    window_start, count = now, 0
                if count >= resolved.rate_limit_requests:
                    retry_after = max(
                        1,
                        int(
                            resolved.rate_limit_window_seconds
                            - (now - window_start)
                        ),
                    )
                    response = JSONResponse(
                        {"detail": "请求过于频繁"},
                        status_code=429,
                        headers={"Retry-After": str(retry_after)},
                    )
                    response.headers["X-Request-ID"] = request_id
                    return response
                rate_buckets[identity] = (window_start, count + 1)
                if len(rate_buckets) > 10_000:
                    stale = [
                        key
                        for key, (started, _) in rate_buckets.items()
                        if now - started >= resolved.rate_limit_window_seconds
                    ]
                    for key in stale:
                        rate_buckets.pop(key, None)

        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        response.headers["X-Request-ID"] = request_id
        if not request.url.path.startswith("/console"):
            _log.info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "status": response.status_code,
                        "latency_ms": round(elapsed_ms, 3),
                        "client_ip": (
                            request.client.host
                            if request.client is not None
                            else "unknown"
                        ),
                    },
                    ensure_ascii=False,
                )
            )
        return response

    @app.middleware("http")
    async def console_auth_guard(
        request: Request,
        call_next: Any,  # noqa: ANN401 - starlette middleware 签名
    ) -> Any:  # noqa: ANN401 - 返回 Response
        """控制台鉴权（v1.2 P1）：设置 console_key 后 /console* 全部校验。
        只包控制台——API 面向 Agent 有自己的策略门，探针供 K8s 探活。"""
        settings = app.state.settings
        path = request.url.path
        if not path.startswith("/console"):
            return await call_next(request)

        existing_csrf = request.cookies.get(console.CSRF_COOKIE)
        csrf_token = existing_csrf or secrets.token_urlsafe(32)
        request.state.csrf_token = csrf_token

        if path in (
            "/console/login",
            "/console/logout",
        ):
            response = await call_next(request)
            if existing_csrf is None:
                _set_csrf_cookie(
                    response, csrf_token, secure=settings.environment == "production"
                )
            return response

        key = settings.console_key
        provided = (
            request.headers.get("X-Console-Key")
            or request.cookies.get("guardrail_console_key")
        )
        if key is not None and provided == key:
            request.state.authenticated_approver = (
                settings.console_approver_id or "console"
            )
            response = await call_next(request)
            if existing_csrf is None:
                _set_csrf_cookie(
                    response, csrf_token, secure=settings.environment == "production"
                )
            return response

        approver_key = (
            request.headers.get("X-API-Key")
            or (
                request.headers.get("Authorization", "")[7:].strip()
                if request.headers.get("Authorization", "").lower().startswith("bearer ")
                else None
            )
        )
        if key is None and approver_key in settings.approver_api_keys:
            request.state.authenticated_approver = settings.approver_api_keys[approver_key]
            response = await call_next(request)
            if existing_csrf is None:
                _set_csrf_cookie(
                    response, csrf_token, secure=settings.environment == "production"
                )
            return response

        if key is None and not settings.agent_api_keys and not settings.approver_api_keys:
            response = await call_next(request)
            if existing_csrf is None:
                _set_csrf_cookie(
                    response, csrf_token, secure=settings.environment == "production"
                )
            return response

        detail = (
            "控制台鉴权失败：需要登录或 X-Console-Key"
            if key is not None
            else "控制台鉴权失败：需要 approver API key"
        )
        response = JSONResponse({"detail": detail}, status_code=401)
        if existing_csrf is None:
            _set_csrf_cookie(
                response, csrf_token, secure=settings.environment == "production"
            )
        return response

    @app.middleware("http")
    async def security_headers(
        request: Request,
        call_next: Any,  # noqa: ANN401 - starlette middleware 签名
    ) -> Any:  # noqa: ANN401 - 返回 Response
        response = await call_next(request)
        _apply_security_headers(
            response, production=resolved.environment == "production"
        )
        return response

    app.include_router(obs.router)
    app.include_router(approvals.router)
    return app


app = create_app()
