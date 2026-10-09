.PHONY: test lint fmt shop gateway clean

# --basetemp 固定在项目内：WorkBuddy 沙箱会拦截系统 TMPDIR 下的 mkdir，
# pytest 的 tmp_path fixture 会撞上 EEXIST 而报 PermissionError。
test:
	uv run pytest -v --basetemp=.pytest_tmp/run

lint:
	uv run ruff check .
	uv run python -m guardrail.policy_lint

fmt:
	uv run ruff format . && uv run ruff check --fix .

shop:
	uv run uvicorn shop.main:app --port 8100

gateway:
	uv run uvicorn guardrail.main:app --port 8000

# 表结构变更后重建开发库。本项目尚未发布，不写迁移层（见计划② Task 1 说明）。
clean:
	rm -f data/gateway.db data/shop.db data/demo.db data/demo-shop.db data/demo.db data/demo-shop.db

# -- M5-M11 收尾段（spec §12.4 一条命令系列） --

.PHONY: scenarios demo eval verify sweep maintenance audit-anchor

# 顺序跑完 4 个录屏场景（spec §13），打印每步决策。
scenarios:
	uv run python -m guardrail scenarios

# 起 商城+网关+控制台（进程内商城），浏览器开 http://127.0.0.1:8000/console
demo:
	uv run python -m guardrail demo

# 三方基线对比（无护栏 / 单次判定 / 本项目），打印拦截率与误伤率表
eval:
	uv run python -m guardrail eval

# corp 域三方对比（语料 sourced 自 Bench-Corp 场景语义，v0.3）
eval-corp:
	uv run python -m guardrail eval-corp

# Bench-Corp 20 场景全量转译评测（B4 二期：含 skipped 清单与已知误伤报告）
eval-bench:
	uv run python -m guardrail eval-bench

# 校验审计哈希链完整性
verify:
	uv run python -m guardrail verify

# 清理过期会话与终态业务记录（审计链不自动截断）
maintenance:
	uv run python -m guardrail maintenance

# 输出审计链头锚点；生产环境应把 stdout 交给外部日志或对象存储
audit-anchor:
	uv run python -m guardrail audit-anchor

# 风险预算系数小网格扫描（spec §18.3）
sweep:
	uv run python -m guardrail sweep

# 性能基准（v1.2 P3）：延迟分布 + 并发吞吐
bench:
	uv run python -m guardrail bench

up:
	docker compose up --build
