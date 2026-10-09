from __future__ import annotations

from datetime import UTC, datetime


def now_iso() -> str:
    """全网关唯一的「当前时间」入口。

    审计链、幂等窗口、provenance 登记、会话过期都读它。集中在一处是为了让
    「同一时刻」的判定在测试里可替换——也避免每个模块各写一份
    `datetime.now(UTC).isoformat()`，改格式时漏改一处。
    """
    return datetime.now(UTC).isoformat()
