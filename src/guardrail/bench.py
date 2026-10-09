"""P3:性能基准——「生产可用性」主张的量化依据。

单会话延迟分布 + 并发吞吐。数字随机器变化，用途是给部署方一个量级参考
与回归基线，不是营销数字。

用法：uv run python -m guardrail bench [--sessions 50] [--calls 20]
"""

from __future__ import annotations

import asyncio
import statistics
import tempfile
import time

import httpx
from asgi_lifespan import LifespanManager

from guardrail.config import Settings
from guardrail.main import create_app
from shop.main import create_app as create_shop_app


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. 输入为空时由调用方保证不成立。"""
    ordered = sorted(values)
    rank = max(1, int(len(ordered) * fraction + 0.999999))
    return ordered[min(rank, len(ordered)) - 1]


async def _run_bench(sessions: int, calls: int) -> dict[str, float]:
    tmp = tempfile.mkdtemp(prefix="guardrail-bench-")
    shop_app = create_shop_app(f"{tmp}/shop.db")
    async with LifespanManager(shop_app):
        app = create_app(
            Settings(shop_base_url="http://shop.test",
                     gateway_db_path=f"{tmp}/gateway.db"),
            shop_transport=httpx.ASGITransport(app=shop_app),
        )
        async with LifespanManager(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://bench") as c:
                # 建 sessions 个会话（各自 prime——provenance 门要求）
                sids = []
                for _ in range(sessions):
                    r = await c.post("/v1/sessions", json={
                        "agent_id": "ops_agent", "task_id": "bench"})
                    sids.append(r.json()["session_id"])
                    await c.post("/v1/tools/list_products",
                                 json={"session_id": sids[-1], "args": {}})

                # 单会话串行延迟（读路径，过判定全链）
                latencies: list[float] = []
                for _ in range(calls):
                    t0 = time.perf_counter()
                    await c.post("/v1/tools/list_products",
                                 json={"session_id": sids[0], "args": {}})
                    latencies.append((time.perf_counter() - t0) * 1000)

                # 并发吞吐：sessions 会话 × calls 次读，全部并行
                t0 = time.perf_counter()
                await asyncio.gather(*[
                    c.post("/v1/tools/list_products",
                           json={"session_id": sid, "args": {}})
                    for sid in sids for _ in range(calls)
                ])
                wall = time.perf_counter() - t0
                total = sessions * calls
    p50 = statistics.median(latencies)
    p95 = percentile(latencies, 0.95)
    p99 = percentile(latencies, 0.99)
    return {"p50_ms": p50, "p95_ms": p95, "p99_ms": p99,
            "rps": total / wall, "total": total, "wall_s": wall}


def run_bench(sessions: int, calls: int) -> None:
    m = asyncio.run(_run_bench(sessions, calls))
    print(f"单会话读延迟（n={calls}）: p50 {m['p50_ms']:.1f}ms · "
          f"p95 {m['p95_ms']:.1f}ms · p99 {m['p99_ms']:.1f}ms")
    print(f"并发吞吐: {m['total']} 请求 / {m['wall_s']:.2f}s = "
          f"{m['rps']:.0f} req/s（进程内 ASGI，含判定全链）")
