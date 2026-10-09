import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# 策略文件默认按「仓库根/policies/single_call.yaml」定位，而不是 CWD 相对路径：
# `uvicorn guardrail.main:app` 的 CWD 取决于谁启动它，把策略找不到变成启动失败
# 是 fail-closed 的正确表现，但不该由 CWD 决定。打包成 wheel 后这个路径不存在，
# 部署方必须显式给 GUARDRAIL_POLICY_PATH——同样是 fail-closed。
_DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "policies" / "single_call.yaml"
_DEFAULT_COMBINED_POLICY_PATH = (
    Path(__file__).resolve().parents[2] / "policies" / "combined_risk.yaml"
)
_DEFAULT_PLAN_POLICY_PATH = (
    Path(__file__).resolve().parents[2] / "policies" / "plan_policy.yaml"
)


class Settings(BaseModel):
    environment: Literal["development", "production"] = "development"
    shop_base_url: str = "http://127.0.0.1:8100"
    gateway_db_path: str = "data/gateway.db"
    db_busy_timeout_ms: int = 5000
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60
    max_request_bytes: int = 1_048_576
    retention_days: int = 365
    policy_path: str = str(_DEFAULT_POLICY_PATH)
    combined_policy_path: str = str(_DEFAULT_COMBINED_POLICY_PATH)
    plan_policy_path: str = str(_DEFAULT_PLAN_POLICY_PATH)
    # 评测模式（spec §12.3 三方对比）：full=本项目 / single=只做单次判定 /
    # none=无护栏。只应由评测 runner 设置，生产恒为 full。
    eval_mode: Literal["full", "single", "none"] = "full"

    # 控制台鉴权（v1.2 生产化 P1）：设置后 /console* 要求 X-Console-Key，
    # 未设置保持开发模式全开。探针（/metrics /readyz）不受限。
    console_key: str | None = None
    console_approver_id: str | None = None
    session_ttl_minutes: int = 30
    # API key -> agent_id / approver_id。两组同时有值才启用认证；
    # 默认空表示保持 demo 兼容，不做身份校验。
    agent_api_keys: dict[str, str] = Field(default_factory=dict)
    approver_api_keys: dict[str, str] = Field(default_factory=dict)


def _api_key_map_from_env(name: str) -> dict[str, str]:
    raw = os.getenv(name)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 必须是 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} 必须是 JSON 对象")
    result: dict[str, str] = {}
    for key, identity in parsed.items():
        if not isinstance(key, str) or not key or not isinstance(identity, str) or not identity:
            raise ValueError(f"{name} 的 key 和 identity 都必须是非空字符串")
        result[key] = identity
    return result


@lru_cache
def get_settings() -> Settings:
    """从环境变量加载配置。只读，进程生命周期内不变。"""
    return Settings(
        environment=os.getenv("GUARDRAIL_ENV", "development"),
        shop_base_url=os.getenv("SHOP_BASE_URL", "http://127.0.0.1:8100"),
        gateway_db_path=os.getenv("GATEWAY_DB_PATH", "data/gateway.db"),
        db_busy_timeout_ms=int(os.getenv("GUARDRAIL_DB_BUSY_TIMEOUT_MS", "5000")),
        rate_limit_requests=int(os.getenv("GUARDRAIL_RATE_LIMIT_REQUESTS", "120")),
        rate_limit_window_seconds=int(
            os.getenv("GUARDRAIL_RATE_LIMIT_WINDOW_SECONDS", "60")
        ),
        max_request_bytes=int(os.getenv("GUARDRAIL_MAX_REQUEST_BYTES", "1048576")),
        retention_days=int(os.getenv("GUARDRAIL_RETENTION_DAYS", "365")),
        policy_path=os.getenv("GUARDRAIL_POLICY_PATH", str(_DEFAULT_POLICY_PATH)),
        combined_policy_path=os.getenv(
            "GUARDRAIL_COMBINED_POLICY_PATH", str(_DEFAULT_COMBINED_POLICY_PATH)
        ),
        plan_policy_path=os.getenv(
            "GUARDRAIL_PLAN_POLICY_PATH", str(_DEFAULT_PLAN_POLICY_PATH)
        ),
        session_ttl_minutes=int(os.getenv("SESSION_TTL_MINUTES", "30")),
        console_key=os.getenv("GUARDRAIL_CONSOLE_KEY"),
        console_approver_id=os.getenv("GUARDRAIL_CONSOLE_APPROVER_ID"),
        agent_api_keys=_api_key_map_from_env("GUARDRAIL_AGENT_API_KEYS"),
        approver_api_keys=_api_key_map_from_env("GUARDRAIL_APPROVER_API_KEYS"),
    )
