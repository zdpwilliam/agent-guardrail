from __future__ import annotations

from fastapi import Header, HTTPException, Request

from guardrail.config import Settings


def auth_enabled(settings: Settings) -> bool:
    return bool(settings.agent_api_keys or settings.approver_api_keys)


def validate_auth_settings(settings: Settings) -> None:
    """认证要么完整启用，要么完整关闭；半配置状态必须拒绝启动。"""
    if bool(settings.agent_api_keys) != bool(settings.approver_api_keys):
        raise RuntimeError(
            "认证配置不完整：agent_api_keys 与 approver_api_keys 必须同时配置"
        )
    if settings.environment == "production" and (
        not settings.agent_api_keys
        or not settings.approver_api_keys
        or not settings.console_key
        or not settings.console_approver_id
    ):
        raise RuntimeError(
            "生产环境必须配置 GUARDRAIL_AGENT_API_KEYS、"
            "GUARDRAIL_APPROVER_API_KEYS、GUARDRAIL_CONSOLE_KEY "
            "和 GUARDRAIL_CONSOLE_APPROVER_ID"
        )
    overlap = set(settings.agent_api_keys) & set(settings.approver_api_keys)
    if overlap:
        raise RuntimeError(f"agent 与 approver API key 不能复用：{sorted(overlap)}")


def _extract_api_key(
    authorization: str | None,
    x_api_key: str | None,
) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def require_agent(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> str | None:
    """返回认证后的 agent_id；认证未启用时返回 None，以保持 demo 兼容。"""
    settings: Settings = request.app.state.settings
    if not auth_enabled(settings):
        return None
    key = _extract_api_key(authorization, x_api_key)
    agent_id = settings.agent_api_keys.get(key or "")
    if agent_id is None:
        raise HTTPException(status_code=401, detail="缺少或无效的 agent API key")
    return agent_id


async def require_approver(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> str | None:
    """返回认证后的审批人身份；认证未启用时返回 None。"""
    settings: Settings = request.app.state.settings
    if not auth_enabled(settings):
        return None
    key = _extract_api_key(authorization, x_api_key)
    approver_id = settings.approver_api_keys.get(key or "")
    if approver_id is None:
        raise HTTPException(status_code=401, detail="缺少或无效的 approver API key")
    return approver_id


async def require_actor(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> tuple[str, str] | None:
    """审计/指标等共享读端：agent 或 approver key 均可。"""
    settings: Settings = request.app.state.settings
    if not auth_enabled(settings):
        return None
    key = _extract_api_key(authorization, x_api_key)
    if key in settings.agent_api_keys:
        return ("agent", settings.agent_api_keys[key])
    if key in settings.approver_api_keys:
        return ("approver", settings.approver_api_keys[key])
    raise HTTPException(status_code=401, detail="缺少或无效的 API key")
