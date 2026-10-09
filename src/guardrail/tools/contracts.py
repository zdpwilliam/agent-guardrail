from __future__ import annotations

from typing import Any

import jsonschema

from guardrail.models import ToolSpec


class ArgsValidationError(Exception):
    """参数不满足工具契约。判定链把它归为 invalid_args（HTTP 400）。"""


def validate_args(spec: ToolSpec, args: dict[str, Any]) -> None:
    """按工具声明的 JSON Schema 校验参数。

    错误消息带上字段路径与工具名：Agent 拿到的是 400 与一句人话，而不是
    一坨 jsonschema 的英文校验树。
    """
    validator = jsonschema.Draft202012Validator(spec.args_schema)
    errors = sorted(validator.iter_errors(args), key=lambda e: list(e.path))
    if not errors:
        return
    first = errors[0]
    location = ".".join(str(part) for part in first.path) or "(根)"
    raise ArgsValidationError(f"工具 {spec.name!r} 的参数 {location} 不合法：{first.message}")
