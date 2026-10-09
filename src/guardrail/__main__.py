"""`python -m guardrail` CLI（spec §12.4 一条命令系列）。

子命令：
  scenarios   顺序跑完 4 个演示场景并打印结果表
  demo        起 商城+网关+控制台（进程内商城），--port 可调
  eval        三方基线对比（无护栏 / 单次判定 / 本项目），打印拦截率与误伤率
  verify      校验审计哈希链完整性
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx


def _cmd_scenarios(_: argparse.Namespace) -> int:
    from guardrail.demo import build_demo_stack, print_results, run_scenarios

    async def run() -> bool:
        async with build_demo_stack("data/demo.db") as (_app, client):
            results = await run_scenarios(client)
            return print_results(results)

    return 0 if asyncio.run(run()) else 1


def _cmd_demo(args: argparse.Namespace) -> int:
    import uvicorn

    from guardrail.config import Settings
    from guardrail.main import create_app
    from shop.main import create_app as create_shop_app

    shop_app = create_shop_app(args.db.replace(".db", "-shop.db"))
    app = create_app(
        Settings(shop_base_url="http://shop.inprocess", gateway_db_path=args.db),
        shop_transport=httpx.ASGITransport(app=shop_app),
    )
    print(f"控制台: http://127.0.0.1:{args.port}/console")
    print("商城运行在同一进程内（ASGI transport），无需第二个端口。Ctrl-C 退出。")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def _cmd_eval(_: argparse.Namespace) -> int:
    from guardrail.evaluation import print_table, run_all

    async def run() -> bool:
        table = await run_all()
        return print_table(table)

    return 0 if asyncio.run(run()) else 1


def _cmd_eval_corp(_: argparse.Namespace) -> int:
    from guardrail.evaluation import print_corp_table, run_corp_eval

    async def run() -> bool:
        table = await run_corp_eval()
        return print_corp_table(table)

    return 0 if asyncio.run(run()) else 1


def _cmd_eval_bench(_: argparse.Namespace) -> int:
    from guardrail.evaluation import print_bench_table, run_bench_eval

    async def run() -> bool:
        table = await run_bench_eval()
        return print_bench_table(table)

    return 0 if asyncio.run(run()) else 1


def _cmd_sweep(_: argparse.Namespace) -> int:
    from guardrail.evaluation import run_sweep

    return 0 if asyncio.run(run_sweep()) else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    from guardrail.audit import ChainVerdict
    from guardrail.stores.audit import SqliteAuditSink
    from guardrail.stores.sqlite import SqliteBackend

    async def run() -> ChainVerdict:
        backend = SqliteBackend(args.db)
        await backend.connect()
        try:
            sink = SqliteAuditSink(backend)
            if args.anchor_file:
                anchor = _read_latest_anchor(Path(args.anchor_file))
                return await sink.verify_anchor(
                    int(anchor["seq"]),
                    str(anchor["entry_hash"]),
                )
            return await sink.verify_chain()
        finally:
            await backend.close()

    verdict = asyncio.run(run())
    if verdict.ok:
        print(f"审计链校验：ok，条目 {verdict.checked}")
        return 0
    print(f"审计链校验失败：断裂于 seq={verdict.broken_at_seq}（{verdict.reason}）")
    return 1


def _read_latest_anchor(path: Path) -> dict:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    if not lines:
        raise ValueError(f"锚点文件为空: {path}")
    return json.loads(lines[-1])


def _cmd_audit_anchor(args: argparse.Namespace) -> int:
    from guardrail.clock import now_iso
    from guardrail.stores.audit import SqliteAuditSink
    from guardrail.stores.sqlite import SqliteBackend

    async def run() -> dict:
        backend = SqliteBackend(args.db)
        await backend.connect()
        try:
            seq, entry_hash = await SqliteAuditSink(backend).head()
        finally:
            await backend.close()
        return {
            "seq": seq,
            "entry_hash": entry_hash,
            "timestamp": now_iso(),
        }

    anchor = asyncio.run(run())
    line = json.dumps(anchor, ensure_ascii=False, sort_keys=True)
    if args.anchor_file:
        path = Path(args.anchor_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    else:
        print(line)
    return 0


def _cmd_maintenance(args: argparse.Namespace) -> int:
    from guardrail.config import get_settings
    from guardrail.retention import purge_operational_data
    from guardrail.stores.sqlite import SqliteBackend

    retention_days = args.retention_days or get_settings().retention_days
    now = datetime.now(UTC)
    cutoff = (now - timedelta(days=retention_days)).isoformat()

    async def run() -> dict[str, int]:
        backend = SqliteBackend(args.db)
        await backend.connect()
        try:
            return await purge_operational_data(
                backend,
                cutoff=cutoff,
                now=now.isoformat(),
            )
        finally:
            await backend.close()

    counts = asyncio.run(run())
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="guardrail", description="会话风险护栏")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("scenarios", help="顺序跑完 4 个演示场景")
    p_demo = sub.add_parser("demo", help="起 商城+网关+控制台")
    p_demo.add_argument("--port", type=int, default=8000)
    p_demo.add_argument("--db", default="data/demo.db")
    sub.add_parser("eval", help="三方基线对比评测")
    sub.add_parser("eval-corp", help="corp 域三方对比（Bench-Corp 来源语料）")
    sub.add_parser("eval-bench", help="Bench-Corp 20 场景全量处置评测")
    sub.add_parser("sweep", help="风险预算系数小网格扫描")
    bench = sub.add_parser("bench", help="性能基准：单会话延迟分布 + 并发吞吐")
    bench.add_argument("--sessions", type=int, default=50)
    bench.add_argument("--calls", type=int, default=20)
    verify = sub.add_parser("verify", help="校验审计哈希链完整性")
    verify.add_argument("--db", default="data/gateway.db")
    verify.add_argument(
        "--anchor-file",
        help="可选：使用 audit-anchor 生成的 JSONL 最后一行作为外部锚点",
    )
    anchor = sub.add_parser("audit-anchor", help="输出或追加审计链头外部锚点")
    anchor.add_argument("--db", default="data/gateway.db")
    anchor.add_argument(
        "--anchor-file",
        help="追加 JSONL 锚点；省略时只打印到 stdout，交给外部日志系统保存",
    )
    maintenance = sub.add_parser("maintenance", help="按保留期清理运维数据")
    maintenance.add_argument("--db", default="data/gateway.db")
    maintenance.add_argument("--retention-days", type=int, default=0)
    sub.add_parser("policy-lint", help="策略静态校验（工具/agent 引用）")
    args = parser.parse_args()
    if args.command == "scenarios":
        return _cmd_scenarios(args)
    if args.command == "demo":
        return _cmd_demo(args)
    if args.command == "eval":
        return _cmd_eval(args)
    if args.command == "eval-corp":
        return _cmd_eval_corp(args)
    if args.command == "eval-bench":
        return _cmd_eval_bench(args)
    if args.command == "sweep":
        return _cmd_sweep(args)
    if args.command == "bench":
        from guardrail.bench import run_bench
        run_bench(args.sessions, args.calls)
        return 0
    if args.command == "verify":
        return _cmd_verify(args)
    if args.command == "audit-anchor":
        return _cmd_audit_anchor(args)
    if args.command == "maintenance":
        return _cmd_maintenance(args)
    if args.command == "policy-lint":
        from guardrail.policy_lint import main as pl_main

        return pl_main()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
