# 标配能力层 实现计划（单次策略 / 审计链 / Provenance / 幂等）

> **交付状态：已实现并合入 v1.2.0。**
> **同步口径：本文件保留当时的实施步骤与代码快照，不作为当前文件结构、API
> 或测试数量的唯一来源；当前实现以 `src/`、`tests/`、系统设计和 README 为准。**
> **工具口径：本计划描述的是当时 9 个电商工具；当前注册表另有 7 个 corp 域工具。**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 spec §6–§8 的四项标配能力落地，让 `POST /v1/tools/{name}` 从「拿到参数就转发」变成「判定 → 记账 → 执行」的完整网关路径，并复现 spec §13 场景 1。

**Architecture:** 三层新增，全部在网关侧。① 策略层（`policy/`）——YAML 策略即数据，受限表达式求值，按 spec §6.1 的固定判定链顺序产出 allow/deny；② 记账层（`audit.py` + `stores/`）——哈希链审计、幂等登记、provenance 登记，三者共用一套 SQLite 后端与协议；③ 接线层（`api/tools.py`）——把三层按「判定 → 幂等 → 审计（先于执行）→ 执行 → 记账」串成一条路径。**所有失败一律 fail-closed**：策略加载失败拒绝启动、策略求值异常拒绝、审计写失败取消执行。

**Tech Stack:** Python 3.11+ / FastAPI / Pydantic v2 / aiosqlite / httpx / PyYAML / jsonschema / pytest + pytest-asyncio / uv / ruff

**前置：** [商城与工具层](02-shop-and-tool-layer.md) 已完成，分支
`feat/m1-shop-and-tool-layer`，工作区干净。本计划在其上继续，**不重做**
商城与投影。

## Global Constraints

- Python 版本下限：`3.11`
- 金额一律用**整数分**。策略文件里的 `max` 也是整数分（`50000` 分 = 500.00 元）
- 时间戳一律 ISO 8601 带时区文本，如 `2026-10-03T14:22:05+00:00`
- ruff 配置沿用计划 ①：`line-length = 100`，`select = ["E","F","I","UP","B","SIM","N","ANN"]`，`target-version = "py311"`
- 工具数量固定 9 个，效果声明与参数契约都写在网关侧 `tools/registry.py`（spec §4.3），工具实现 `handlers.py` 仍然不含任何护栏逻辑
- **fail-closed 无例外**：策略文件缺失/解析失败/lint 不过 → 进程拒绝启动；策略表达式求值异常 → 拒绝该次调用；审计链写入失败 → 取消执行；投影失败 → 拒绝
- 受限表达式**不引入 CEL 或任何表达式引擎**：白名单 AST 求值，只允许 `args.<字段>` 形式的属性访问
- 状态一律外置，不留进程内单例缓存（spec §19.2）。`lru_cache` 只用于配置与策略
- 本计划内**不实现**组合风险、风险预算、计划生命周期、控制台——那些属于计划 ③④⑤

---

## File Structure

| 文件 | 职责 |
|---|---|
| `pyproject.toml` | 新增 `pyyaml`、`jsonschema` 依赖 |
| `Makefile` | 新增 `clean` 目标 |
| `policies/single_call.yaml` | ★ 单次策略（权限表 + 规则），策略即数据 |
| `src/guardrail/config.py` | 新增 `policy_path`、`session_ttl_minutes` |
| `src/guardrail/clock.py` | 统一的 `now_iso()` |
| `src/guardrail/models.py` | `ToolSpec` 新增 `args_schema` / `emits` / `requires` |
| `src/guardrail/tools/contracts.py` | 参数 JSON Schema 校验（`ArgsValidationError`） |
| `src/guardrail/tools/registry.py` | 9 个工具的参数契约、实体产出、实体依赖；自检扩展 |
| `src/guardrail/policy/__init__.py` | 空 |
| `src/guardrail/policy/expr.py` | ★ 受限表达式求值器 |
| `src/guardrail/policy/single.py` | 策略模型（`SingleCallPolicy` / `SingleRule` / `PolicyError`） |
| `src/guardrail/policy/loader.py` | YAML → 策略模型 + lint |
| `src/guardrail/policy/lint.py` | 启动期策略自检 |
| `src/guardrail/policy/engine.py` | ★ 判定链编排，产出 `SingleVerdict` |
| `src/guardrail/policy/caps.py` | ★ 结果层上限（从投影结果态读值） |
| `src/guardrail/projection_args.py` | 从 `api/tools.py` 迁出的派生参数计算（避免与策略层循环依赖） |
| `src/guardrail/shadow_loader.py` | 影子装载器，供预览与结果层上限共用 |
| `src/guardrail/provenance.py` | ★ 实体产出登记与写操作前置校验 |
| `src/guardrail/idempotency.py` | 幂等键计算 + `IdempotencyStore` 协议 |
| `src/guardrail/audit.py` | ★ 哈希链：`AuditDraft` / `AuditEntry` / 哈希计算 / `AuditSink` 协议 |
| `src/guardrail/protocols.py` | `SessionStore`（加 `expires_at` / `sweep_expired`）、`DecisionEventBus` |
| `src/guardrail/stores/sqlite.py` | `SqliteBackend`（共享连接）+ 全部表结构 |
| `src/guardrail/stores/audit.py` | `SqliteAuditSink`（append + verify_chain） |
| `src/guardrail/stores/idempotency.py` | `SqliteIdempotencyStore` |
| `src/guardrail/stores/provenance.py` | `SqliteProvenanceStore` |
| `src/guardrail/api/tools.py` | ★ 调用路径重写：判定 → 幂等 → 审计 → 执行 |
| `src/guardrail/api/audit.py` | `GET /v1/audit/verify` |
| `src/guardrail/main.py` | 装配策略与四个存储 |

---

### Task 1: 存储协议接缝与共享 SQLite 后端

**Files:**
- Modify: `pyproject.toml`（新增两个依赖）
- Modify: `src/guardrail/protocols.py`
- Modify: `src/guardrail/stores/sqlite.py`
- Create: `src/guardrail/clock.py`
- Modify: `src/guardrail/config.py`
- Modify: `src/guardrail/api/sessions.py`
- Modify: `Makefile`
- Test: `tests/test_stores.py`
- Test: `tests/test_config.py`（追加 1 条）

**Interfaces:**
- Consumes: 计划 ① 的 `SessionRecord`（本任务给它加 `expires_at`）
- Produces:
  - `guardrail.clock.now_iso() -> str`
  - `guardrail.protocols.SessionRecord`（新增 `expires_at: str`）
  - `guardrail.protocols.SessionStore`（新增 `async sweep_expired(now_iso: str) -> int`）
  - `guardrail.protocols.DecisionEvent`、`DecisionEventBus`
  - `guardrail.stores.sqlite.SqliteBackend`：`__init__(db_path)`、`async connect()`、`async close()`、属性 `conn`、`async fetchall(sql, params=())`、`async fetchone(sql, params=())`、`async execute(sql, params=())`、`async commit()`
  - `guardrail.stores.sqlite.SqliteSessionStore`（改为接收 `SqliteBackend`）：`async load`、`async save`、`async sweep_expired`
  - `guardrail.config.Settings`（新增 `policy_path: str`、`session_ttl_minutes: int = 30`）
  - `guardrail.config.get_settings()`（读 `GUARDRAIL_POLICY_PATH`、`SESSION_TTL_MINUTES`）
- 表结构：`sessions`（加 `expires_at`）、`provenance`、`idempotency`、`audit_log`（加 `replay`）

- [x] **Step 1: 加依赖**

`pyproject.toml` 的 `dependencies` 改为：

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "aiosqlite>=0.20",
    "httpx>=0.27",
    "pyyaml>=6.0",
    "jsonschema>=4.23",
]
```

- [x] **Step 2: 写失败测试 `tests/test_stores.py`**

```python
import pytest

from guardrail.clock import now_iso
from guardrail.protocols import SessionRecord
from guardrail.stores.sqlite import SqliteBackend, SqliteSessionStore


@pytest.fixture
async def backend(tmp_path):
    b = SqliteBackend(str(tmp_path / "gateway.db"))
    await b.connect()
    yield b
    await b.close()


async def test_connect_creates_all_guardrail_tables(backend):
    rows = await backend.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    assert {"sessions", "provenance", "idempotency", "audit_log"} <= names


async def test_sessions_has_expires_at_column(backend):
    cols = await backend.fetchall("PRAGMA table_info(sessions)")
    assert "expires_at" in {c["name"] for c in cols}


async def test_audit_log_has_replay_column(backend):
    cols = await backend.fetchall("PRAGMA table_info(audit_log)")
    types = {c["name"]: c["type"] for c in cols}
    assert types["replay"] == "INTEGER"
    assert types["entry_hash"] == "TEXT"


async def test_provenance_primary_key_is_session_scoped(backend):
    import sqlite3

    await backend.execute(
        "INSERT INTO provenance (session_id, entity_type, entity_id, issued_at)"
        " VALUES ('s1', 'product', 'p1', ?)",
        (now_iso(),),
    )
    await backend.commit()
    # 同一会话重复登记是 INSERT OR IGNORE 的职责，裸 INSERT 必须报唯一约束冲突。
    with pytest.raises(sqlite3.IntegrityError):
        await backend.execute(
            "INSERT INTO provenance (session_id, entity_type, entity_id, issued_at)"
            " VALUES ('s1', 'product', 'p1', ?)",
            (now_iso(),),
        )


def _record(session_id: str = "s-1", expires_at: str | None = None) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        agent_id="ops_agent",
        task_id=None,
        created_at=now_iso(),
        expires_at=expires_at or now_iso(),
    )


async def test_session_store_roundtrip(backend):
    store = SqliteSessionStore(backend)
    await store.save(_record())
    loaded = await store.load("s-1")
    assert loaded is not None
    assert loaded.agent_id == "ops_agent"
    assert loaded.expires_at


async def test_session_store_load_missing_returns_none(backend):
    assert await SqliteSessionStore(backend).load("s-nope") is None


async def test_sweep_expired_removes_only_expired(backend):
    store = SqliteSessionStore(backend)
    await store.save(_record("s-live", expires_at="2999-01-01T00:00:00+00:00"))
    await store.save(_record("s-dead", expires_at="2000-01-01T00:00:00+00:00"))
    removed = await store.sweep_expired(now_iso())
    assert removed == 1
    assert await store.load("s-live") is not None
    assert await store.load("s-dead") is None


def test_now_iso_is_timezone_aware_utc():
    from datetime import datetime

    parsed = datetime.fromisoformat(now_iso())
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
```

- [x] **Step 3: 跑测试确认失败**

Run: `uv sync && uv run pytest tests/test_stores.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'guardrail.stores.sqlite'` 之类的导入错误（`guardrail.clock` 尚不存在）

- [x] **Step 4: 写 `src/guardrail/clock.py`**

```python
from __future__ import annotations

from datetime import UTC, datetime


def now_iso() -> str:
    """全网关唯一的「当前时间」入口。

    审计链、幂等窗口、provenance 登记、会话过期都读它。集中在一处是为了让
    「同一时刻」的判定在测试里可替换——也避免每个模块各写一份
    `datetime.now(UTC).isoformat()`，改格式时漏改一处。
    """
    return datetime.now(UTC).isoformat()
```

- [x] **Step 5: 改 `src/guardrail/protocols.py`**

```python
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class SessionRecord(BaseModel):
    session_id: str
    agent_id: str
    task_id: str | None = None
    created_at: str
    # 会话有效期（spec §3.1，默认 30 分钟）。判定链第 1 步读它：
    # 一个过期的会话必须被拒绝，否则「组合风险」的载体在时间上就是漏的。
    expires_at: str


class SessionStore(Protocol):
    """会话存储协议。

    当前唯一实现是 SQLite（单进程）。多进程扩展只需新增一个实现——
    调用方代码不变。见 spec §19.1 / §19.2。
    """

    async def load(self, session_id: str) -> SessionRecord | None: ...

    async def save(self, record: SessionRecord) -> None: ...

    async def sweep_expired(self, now_iso: str) -> int:
        """删除已过期会话，返回删除条数。"""
        ...


class DecisionEvent(BaseModel):
    session_id: str
    tool: str
    decision: str
    reasons: list[str]
    timestamp: str


class DecisionEventBus(Protocol):
    """决策事件总线（spec §19.1 的第三个接缝）。

    控制台（M5）需要看到待审批项；单进程下同步调用就够。保留协议是为了将来
    换 Redis Pub/Sub 时调用方代码一行不改。
    """

    def publish(self, event: DecisionEvent) -> None: ...
```

- [x] **Step 6: 重写 `src/guardrail/stores/sqlite.py`**

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

from guardrail.protocols import SessionRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  task_id TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_task ON sessions(task_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS provenance (
  session_id TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  issued_at TEXT NOT NULL,
  PRIMARY KEY (session_id, entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  status TEXT NOT NULL,            -- in_progress | done
  response_json TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  prev_hash TEXT NOT NULL,
  entry_hash TEXT NOT NULL,
  session_id TEXT NOT NULL,
  plan_id TEXT,
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL,
  decision TEXT NOT NULL,          -- allow | allow_with_flag | ask | deny
  replay INTEGER NOT NULL DEFAULT 0,
  reasons_json TEXT NOT NULL,
  timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
"""


class SqliteBackend:
    """网关侧 SQLite 的共享连接。

    四个存储（会话 / 审计 / 幂等 / provenance）共用一个连接，而不是各开一个：
    多个连接写同一个文件会撞上 SQLite 的写锁，而单进程下没有理由付这个代价。
    多进程扩展时这一步会变成连接池，调用方（各 Store）代码不变。
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteBackend.connect() 未调用")
        return self._conn

    async def fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def fetchone(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> aiosqlite.Row | None:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)

    async def commit(self) -> None:
        await self.conn.commit()


class SqliteSessionStore:
    def __init__(self, backend: SqliteBackend) -> None:
        self.backend = backend

    async def load(self, session_id: str) -> SessionRecord | None:
        row = await self.backend.fetchone("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        return SessionRecord(
            session_id=row["id"],
            agent_id=row["agent_id"],
            task_id=row["task_id"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    async def save(self, record: SessionRecord) -> None:
        await self.backend.execute(
            "INSERT OR REPLACE INTO sessions (id, agent_id, task_id, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                record.session_id,
                record.agent_id,
                record.task_id,
                record.created_at,
                record.expires_at,
            ),
        )
        await self.backend.commit()

    async def sweep_expired(self, now_iso: str) -> int:
        """删掉已过期会话。

        ISO 8601 带同一时区偏移的文本字典序即时间序，所以字符串比较就够了——
        前提是所有写入都带时区（`clock.now_iso()` 保证这一点）。
        """
        cur = await self.backend.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_iso,))
        await self.backend.commit()
        return cur.rowcount if cur.rowcount is not None else 0
```

> **开发期数据库不迁移**：这是当时尚未发布阶段的实现选择，`make clean`
> 负责重建开发库。当前 v1.2.0 已引入 `schema_migrations`，生产升级以
> `src/guardrail/stores/sqlite.py` 和 `deploy/README.md` 为准。

- [x] **Step 7: 改 `src/guardrail/config.py`**

```python
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

# 策略文件默认按「仓库根/policies/single_call.yaml」定位，而不是 CWD 相对路径：
# `uvicorn guardrail.main:app` 的 CWD 取决于谁启动它，把策略找不到变成启动失败
# 是 fail-closed 的正确表现，但不该由 CWD 决定。打包成 wheel 后这个路径不存在，
# 部署方必须显式给 GUARDRAIL_POLICY_PATH——同样是 fail-closed。
_DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "policies" / "single_call.yaml"


class Settings(BaseModel):
    shop_base_url: str = "http://127.0.0.1:8100"
    gateway_db_path: str = "data/gateway.db"
    policy_path: str = str(_DEFAULT_POLICY_PATH)
    session_ttl_minutes: int = 30


@lru_cache
def get_settings() -> Settings:
    """从环境变量加载配置。只读，进程生命周期内不变。"""
    import os

    return Settings(
        shop_base_url=os.getenv("SHOP_BASE_URL", "http://127.0.0.1:8100"),
        gateway_db_path=os.getenv("GATEWAY_DB_PATH", "data/gateway.db"),
        policy_path=os.getenv("GUARDRAIL_POLICY_PATH", str(_DEFAULT_POLICY_PATH)),
        session_ttl_minutes=int(os.getenv("SESSION_TTL_MINUTES", "30")),
    )
```

- [x] **Step 8: 改 `src/guardrail/api/sessions.py`**

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from pydantic import BaseModel

from guardrail.clock import now_iso
from guardrail.config import get_settings
from guardrail.protocols import SessionRecord

router = APIRouter(prefix="/v1", tags=["sessions"])


class SessionCreate(BaseModel):
    agent_id: str
    task_id: str | None = None


@router.post("/sessions")
async def create_session(request: Request, body: SessionCreate) -> dict[str, str | None]:
    """开一个会话，并顺手清掉过期的旧会话。

    没有后台调度器（单进程 demo，见 spec §18.2），所以清理挂在建会话这个低频
    写操作上。真实部署里这一步会换成定时任务，Store 接口不变。
    """
    store = request.app.state.sessions
    await store.sweep_expired(now_iso())

    now = datetime.now(UTC)
    record = SessionRecord(
        session_id=f"s-{uuid.uuid4().hex[:12]}",
        agent_id=body.agent_id,
        task_id=body.task_id,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=get_settings().session_ttl_minutes)).isoformat(),
    )
    await store.save(record)
    return record.model_dump()
```

- [x] **Step 9: 改 `Makefile`**

```makefile
.PHONY: test lint fmt shop gateway clean

test:
	uv run pytest -v

lint:
	uv run ruff check .

fmt:
	uv run ruff format . && uv run ruff check --fix .

shop:
	uv run uvicorn shop.main:app --port 8100

gateway:
	uv run uvicorn guardrail.main:app --port 8000

# 表结构变更后重建开发库。本项目尚未发布，不写迁移层（见 Task 1 说明）。
clean:
	rm -f data/gateway.db data/shop.db
```

- [x] **Step 10: 追加一条配置测试**

在 `tests/test_config.py` 末尾追加：

```python
def test_default_policy_path_points_into_repo_policies():
    from pathlib import Path

    s = Settings()
    assert Path(s.policy_path).name == "single_call.yaml"
    assert Path(s.policy_path).parent.name == "policies"
    assert Path(s.policy_path).is_absolute()


def test_settings_reads_policy_path_and_ttl(monkeypatch):
    monkeypatch.setenv("GUARDRAIL_POLICY_PATH", "/tmp/p.yaml")
    monkeypatch.setenv("SESSION_TTL_MINUTES", "5")
    get_settings.cache_clear()
    s = get_settings()
    assert s.policy_path == "/tmp/p.yaml"
    assert s.session_ttl_minutes == 5
    get_settings.cache_clear()
```

- [x] **Step 11: 跑测试确认通过**

Run: `uv run pytest tests/test_stores.py tests/test_config.py -v`
Expected: 13 passed（stores 8 + config 5）

- [x] **Step 12: 修 `main.py` 的构造签名**

`SqliteSessionStore` 构造参数变了，`main.py` 暂时改回能跑的形式（Task 10 会整体重写）：

```python
        backend = SqliteBackend(resolved.gateway_db_path)
        await backend.connect()
        store = SqliteSessionStore(backend)
        app.state.backend = backend
        app.state.sessions = store
```

并在 import 段加 `from guardrail.stores.sqlite import SqliteBackend, SqliteSessionStore`。

- [x] **Step 13: 跑全量测试确认没有回归**

Run: `uv run pytest -v`
Expected: 全部通过（计划 ① 的 14 个测试文件仍全绿）

- [x] **Step 14: Commit**

```bash
git add pyproject.toml Makefile src/guardrail/clock.py src/guardrail/config.py \
  src/guardrail/protocols.py src/guardrail/stores/sqlite.py src/guardrail/api/sessions.py \
  src/guardrail/main.py tests/test_stores.py tests/test_config.py uv.lock
git commit -m "feat: 存储协议接缝——共享 SQLite 后端、四表结构、会话有效期与决策事件协议"
```

---

### Task 2: 受限表达式求值器

**Files:**
- Create: `src/guardrail/policy/__init__.py`（空）
- Create: `src/guardrail/policy/expr.py`
- Test: `tests/test_expr.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `guardrail.policy.expr.ExpressionError`
  - `guardrail.policy.expr.MAX_NODES: int`
  - `guardrail.policy.expr.parse(expression: str) -> ast.Expression`
  - `guardrail.policy.expr.arg_names(expression: str) -> set[str]`
  - `guardrail.policy.expr.evaluate(expression: str, args: dict) -> Any`

- [x] **Step 1: 写失败测试 `tests/test_expr.py`**

```python
import pytest

from guardrail.policy.expr import (
    MAX_NODES,
    ExpressionError,
    arg_names,
    evaluate,
    parse,
)


# ---------- 正常求值 ----------

@pytest.mark.parametrize(
    ("expression", "args", "expected"),
    [
        ("abs(args.delta_pct) > 10", {"delta_pct": -50}, True),
        ("abs(args.delta_pct) > 10", {"delta_pct": -5}, False),
        ("args.discount_pct > 80", {"discount_pct": 90.0}, True),
        ("args.qty >= 1 and args.qty <= 10", {"qty": 3}, True),
        ("args.qty >= 1 and args.qty <= 10", {"qty": 30}, False),
        ("min(args.a, args.b) <= 0", {"a": 5, "b": 0}, True),
        ("max(args.a, args.b) > 100", {"a": 5, "b": 200}, True),
        ("len(args.code) > 0", {"code": "S20"}, True),
        ("round(args.pct) == 20", {"pct": 19.6}, True),
        ("args.qty in [1, 2, 3]", {"qty": 2}, True),
        ("args.qty in [1, 2, 3]", {"qty": 9}, False),
        ("args.flag", {"flag": False}, False),
        ("not args.flag", {"flag": False}, True),
        ("args.a if args.b else args.c", {"a": 1, "b": True, "c": 2}, 1),
    ],
)
def test_evaluate_supported_expressions(expression, args, expected):
    assert evaluate(expression, args) == expected


def test_absent_optional_arg_reads_as_none():
    # args.coupon_id 在 schema 里是可选参数，模型不下发时必须读作 None，
    # 否则 `args.coupon_id != null` 会在无券订单上直接求值失败。
    assert evaluate("args.coupon_id != null", {}) is False
    assert evaluate("args.coupon_id != null", {"coupon_id": "c-1"}) is True


@pytest.mark.parametrize(
    ("expression", "args"),
    [
        ("abs(args.missing)", {}),
        ("args.a + args.b", {"a": "x", "b": 1}),
        ("args.a > 1", {"a": None}),
    ],
)
def test_evaluate_runtime_error_becomes_expression_error(expression, args):
    # 求值期出错必须变成 ExpressionError，调用方据此 fail-closed 拒绝，
    # 绝不能让 TypeError 逃逸成 500。
    with pytest.raises(ExpressionError):
        evaluate(expression, args)


# ---------- YAML 风格的字面量 ----------

def test_yaml_style_literals_are_bound():
    assert evaluate("args.x == null", {"x": None}) is True
    assert evaluate("args.x == true", {"x": True}) is True
    assert evaluate("args.x == false", {"x": False}) is True


# ---------- 沙箱逃逸 ----------

@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('ls')",
        "args.__class__",
        "args.__class__.__mro__",
        "open('/etc/passwd')",
        "eval('1+1')",
        "args.__dict__",
        "[x for x in args]",
        "(lambda: 1)()",
        "args.a if args.b else __import__('os')",
        "f'{args.a}'",
        "args.a.b",
        "args['a']",
        "args.a; import os",
    ],
)
def test_evaluate_rejects_escapes(expression):
    with pytest.raises(ExpressionError):
        evaluate(expression, {"a": 1, "b": True})


def test_evaluate_rejects_unknown_function():
    with pytest.raises(ExpressionError):
        evaluate("dir(args)", {})


def test_evaluate_rejects_keyword_arguments():
    with pytest.raises(ExpressionError):
        evaluate("round(args.x, ndigits=2)", {"x": 1.234})


def test_evaluate_rejects_unknown_name():
    with pytest.raises(ExpressionError):
        evaluate("args.x + secret", {"x": 1})


def test_evaluate_rejects_syntax_error():
    with pytest.raises(ExpressionError):
        evaluate("args.x >", {"x": 1})


def test_builtins_are_not_reachable_even_if_name_check_were_skipped():
    # 名字白名单是第一道防线，清空 builtins 是第二道：两道都要在。
    assert "__builtins__" not in dir(evaluate)
    with pytest.raises(ExpressionError):
        evaluate("__builtins__", {})


# ---------- 复杂度上限 ----------

def test_expression_node_count_is_capped():
    bomb = "1" + "".join(f" + {i}" for i in range(MAX_NODES + 10))
    with pytest.raises(ExpressionError, match="过于复杂"):
        evaluate(bomb, {})


def test_parse_returns_expression_node():
    import ast

    assert isinstance(parse("args.x > 1"), ast.Expression)


def test_arg_names_extracts_referenced_fields():
    assert arg_names("abs(args.delta_pct) > 10 and args.qty < 3") == {"delta_pct", "qty"}
    assert arg_names("args.x == null") == {"x"}
    assert arg_names("1 > 0") == set()


def test_arg_names_rejects_invalid_expression():
    with pytest.raises(ExpressionError):
        arg_names("args.__class__ > 1")
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_expr.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'guardrail.policy'`

- [x] **Step 3: 写 `src/guardrail/policy/__init__.py`（空文件）与 `src/guardrail/policy/expr.py`**

```python
from __future__ import annotations

import ast
from typing import Any

# 节点数上限。一条策略表达式正常在 10 个节点以内；300 个节点足够表达任何
# 业务阈值，同时让「构造一颗解析炸弹」这件事在加载期就被挡住，而不是在
# 每次调用时烧 CPU。
MAX_NODES = 300

# 函数白名单。刻意保持极小：策略表达式只需要这几个，多一个就多一个能被
# 用来做类型混淆或算术放大的入口。
_ALLOWED_FUNCS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "len": len,
    "round": round,
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
}

# YAML/JSON 风格的字面量。策略文件读起来更像配置而不是 Python——
# spec §3.4 的样例写的就是 `args.coupon_id != null`。
_CONSTANTS: dict[str, Any] = {"null": None, "true": True, "false": False}

_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.UAdd,
    ast.USub,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Attribute,
    ast.IfExp,
    ast.Tuple,
    ast.List,
)


class ExpressionError(Exception):
    """表达式非法或求值失败。调用方必须按 fail-closed 处理（spec §10.2）。"""


def _check_attribute(node: ast.Attribute) -> None:
    """只允许 `args.<字段>`。

    属性访问是沙箱逃逸的主要入口（`().__class__.__bases__` 一路走到 object
    就能拿到一切）。所以这里不是「禁掉属性访问」，而是把接收者钉死为 `args`，
    并禁掉下划线开头的名字。
    """
    if not isinstance(node.value, ast.Name) or node.value.id != "args":
        raise ExpressionError("只允许访问 args 的字段")
    if node.attr.startswith("_"):
        raise ExpressionError(f"禁止访问下划线开头的字段：{node.attr}")


def _check_name(node: ast.Name) -> None:
    if node.id == "args" or node.id in _ALLOWED_FUNCS or node.id in _CONSTANTS:
        return
    raise ExpressionError(f"表达式引用了未知名字：{node.id}")


def _check_call(node: ast.Call) -> None:
    # 只允许 `f(...)` 的直接形式：`args.foo()` 这类带接收者的调用已被
    # _check_attribute 拦下，*args / **kwargs 也一并拒绝。
    if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
        raise ExpressionError("只允许调用白名单内的函数")
    if node.keywords:
        raise ExpressionError("表达式不支持关键字参数")


def _validate(tree: ast.Expression) -> None:
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise ExpressionError(f"表达式过于复杂：{len(nodes)} 个节点，上限 {MAX_NODES}")
    for node in nodes:
        if not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(f"表达式含不允许的语法：{type(node).__name__}")
        if isinstance(node, ast.Attribute):
            _check_attribute(node)
        elif isinstance(node, ast.Name):
            _check_name(node)
        elif isinstance(node, ast.Call):
            _check_call(node)


def parse(expression: str) -> ast.Expression:
    """解析并静态校验一条策略表达式。"""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"表达式语法错误：{exc.msg}") from exc
    _validate(tree)
    return tree


def arg_names(expression: str) -> set[str]:
    """表达式引用了哪些 `args` 字段。

    策略 lint 用它对照工具的参数契约抓拼写错误（`args.delt_pct` 这类）——
    没有这一步，拼错的字段会静默读作 None，规则从此永不触发。
    """
    return {n.attr for n in ast.walk(parse(expression)) if isinstance(n, ast.Attribute)}


class _Args:
    """表达式看到的 args 视图。

    缺失的可选参数读作 `None`——这样 `args.coupon_id != null` 才能在「没带券」
    的订单上正常求值。真正的类型错误（比如 `abs(None)`）会在 evaluate 里变成
    ExpressionError，由调用方 fail-closed。拼写错误由 lint 拦，不靠这里。
    """

    __slots__ = ("_values",)

    def __init__(self, values: dict[str, Any]) -> None:
        object.__setattr__(self, "_values", values)

    def __getattr__(self, name: str) -> Any:
        # 只在常规属性查找失败时触发，因此不会与 _values 打架。
        if name.startswith("_"):
            raise ExpressionError(f"禁止访问下划线开头的字段：{name}")
        return self._values.get(name)


def evaluate(expression: str, args: dict[str, Any]) -> Any:
    """求值一条策略表达式。静态校验 + 动态求值，任一环节出错都抛 ExpressionError。"""
    tree = parse(expression)
    namespace: dict[str, Any] = {
        **_ALLOWED_FUNCS,
        **_CONSTANTS,
        "args": _Args(args),
        # 名字白名单已经是第一道防线，这里清空 builtins 是第二道。eval 会
        # 往 globals 里塞 __builtins__，不清空就等于把 open / __import__
        # 交到一个「万一白名单被绕过」的表达式手里。
        "__builtins__": {},
    }
    try:
        return eval(compile(tree, filename="<policy>", mode="eval"), namespace)  # noqa: S307
    except ExpressionError:
        raise
    except Exception as exc:
        raise ExpressionError(f"表达式求值失败：{type(exc).__name__}: {exc}") from exc
```

> **`noqa: S307` 为什么留着**：ruff 当前的 `select` 没开 `S`（flake8-bandit），这行注释是给开启 bandit 的读者看的——`eval` 在这里是**有意为之**，安全性由上面两道防线保证，不来自「不用 eval」。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_expr.py -v`
Expected: 43 passed

- [x] **Step 5: Commit**

```bash
git add src/guardrail/policy/__init__.py src/guardrail/policy/expr.py tests/test_expr.py
git commit -m "feat: 受限表达式求值器——白名单 AST + 清空 builtins 的双道防线"
```

---

### Task 3: 工具参数契约与实体引用声明

**Files:**
- Modify: `src/guardrail/models.py`
- Modify: `src/guardrail/tools/registry.py`
- Create: `src/guardrail/tools/contracts.py`
- Test: `tests/test_tool_contracts.py`
- Modify: `tests/test_tool_registry.py`（`_specs_with_effect` 改为基于真实 spec 派生）

**Interfaces:**
- Consumes: 计划 ① 的 `ToolSpec`、`TOOL_SPECS`、`assert_specs_valid`
- Produces:
  - `guardrail.models.EntityEmit(entity_type: Literal["product","coupon","order"], path: str)`
  - `guardrail.models.EntityRequire(entity_type, arg: str, optional: bool = False)`
  - `guardrail.models.ToolSpec` 新增 `args_schema: dict`、`emits: list[EntityEmit]`、`requires: list[EntityRequire]`
  - `guardrail.tools.contracts.ArgsValidationError`
  - `guardrail.tools.contracts.validate_args(spec: ToolSpec, args: dict) -> None`
  - `guardrail.tools.registry.assert_specs_valid()` 扩展为同时校验参数契约、产出路径、实体依赖

- [x] **Step 1: 写失败测试 `tests/test_tool_contracts.py`**

```python
import pytest

from guardrail.models import EntityEmit, EntityRequire
from guardrail.tools.contracts import ArgsValidationError, validate_args
from guardrail.tools.registry import TOOL_SPECS


def _spec(tool: str):
    return TOOL_SPECS[tool]


# ---------- 正常 ----------

def test_valid_update_price_args_pass():
    validate_args(_spec("update_price"), {"product_id": "p-1", "delta_pct": -5.0})


def test_optional_coupon_id_may_be_absent_or_null():
    validate_args(_spec("create_order"), {"product_id": "p-1", "qty": 1})
    validate_args(_spec("create_order"), {"product_id": "p-1", "qty": 1, "coupon_id": None})
    validate_args(_spec("create_order"), {"product_id": "p-1", "qty": 1, "coupon_id": "c-1"})


def test_list_products_accepts_empty_args():
    validate_args(_spec("list_products"), {})


# ---------- 违规 ----------

def test_missing_required_arg_raises():
    with pytest.raises(ArgsValidationError, match="product_id"):
        validate_args(_spec("get_product"), {})


def test_unknown_arg_raises():
    # 未知参数一律拒绝：模型多报一个字段通常意味着它在编造，而服务端派生的
    # 字段（absolute_delta_cents）绝不能由模型自己塞进来。
    with pytest.raises(ArgsValidationError):
        validate_args(_spec("update_price"), {"product_id": "p", "delta_pct": -1, "extra": 1})


def test_model_cannot_inject_server_derived_field():
    with pytest.raises(ArgsValidationError):
        validate_args(
            _spec("update_price"),
            {"product_id": "p", "delta_pct": -1, "absolute_delta_cents": -999999},
        )


def test_string_delta_pct_raises():
    with pytest.raises(ArgsValidationError):
        validate_args(_spec("update_price"), {"product_id": "p", "delta_pct": "-5"})


def test_non_integer_qty_raises():
    with pytest.raises(ArgsValidationError, match="qty"):
        validate_args(_spec("create_order"), {"product_id": "p", "qty": 1.5})


def test_zero_qty_raises():
    with pytest.raises(ArgsValidationError, match="qty"):
        validate_args(_spec("create_order"), {"product_id": "p", "qty": 0})


def test_empty_product_id_raises():
    with pytest.raises(ArgsValidationError, match="product_id"):
        validate_args(_spec("get_product"), {"product_id": ""})


def test_discount_pct_out_of_range_raises():
    with pytest.raises(ArgsValidationError, match="discount_pct"):
        validate_args(_spec("create_coupon"), {"code": "X", "discount_pct": 120.0, "max_uses": 1})


def test_error_message_names_the_offending_field():
    with pytest.raises(ArgsValidationError) as exc:
        validate_args(_spec("create_coupon"), {"code": "X", "discount_pct": 20.0, "max_uses": 0})
    assert "max_uses" in str(exc.value)


# ---------- 契约本身的完整性 ----------

def test_every_tool_declares_args_schema():
    for name, spec in TOOL_SPECS.items():
        assert spec.args_schema.get("type") == "object", name
        assert spec.args_schema.get("additionalProperties") is False, name


def test_every_write_tool_declares_its_entity_requirements():
    assert {r.arg for r in TOOL_SPECS["update_price"].requires} == {"product_id"}
    assert {r.arg for r in TOOL_SPECS["refund_order"].requires} == {"order_id"}
    order_reqs = {r.arg: r for r in TOOL_SPECS["create_order"].requires}
    assert set(order_reqs) == {"product_id", "coupon_id"}
    # 券是可选依赖：没带券就不该要求 coupon_id 已被本会话读过。
    assert order_reqs["coupon_id"].optional is True
    assert order_reqs["product_id"].optional is False


def test_read_tools_have_no_requirements():
    for name in ("list_products", "get_product", "get_order"):
        assert TOOL_SPECS[name].requires == []


def test_emit_paths_point_at_plausible_top_level_keys():
    emits = {e.entity_type: e.path for e in TOOL_SPECS["get_product"].emits}
    assert emits == {"product": "product.id"}
    order = {e.entity_type: e.path for e in TOOL_SPECS["create_order"].emits}
    assert order["order"] == "order_id"
    assert order["product"] == "order.product_id"
    listing = {e.entity_type: e.path for e in TOOL_SPECS["list_products"].emits}
    assert listing == {"product": "products[].id"}


def test_send_email_has_neither_emits_nor_requires():
    assert TOOL_SPECS["send_email"].emits == []
    assert TOOL_SPECS["send_email"].requires == []


def test_entity_models_reject_unknown_type():
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic.ValidationError 的基类断言
        EntityEmit(entity_type="invoice", path="x")


def test_require_defaults_to_mandatory():
    assert EntityRequire(entity_type="product", arg="product_id").optional is False
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_tool_contracts.py -v`
Expected: FAIL —— `ImportError: cannot import name 'EntityEmit' from 'guardrail.models'`

- [x] **Step 3: 在 `src/guardrail/models.py` 加两个模型并扩展 `ToolSpec`**

在 `EffectOp` 定义之前插入：

```python
class EntityEmit(BaseModel):
    """工具返回值里实体 id 的位置。

    path 语法：`coupon.id`、`products[].id`、`order_id`。刻意用声明式路径，
    而不是「递归扫返回值里所有像 id 的字符串」——扫描会把商品名、邮箱域名
    也登记成 provenance，那道门就形同虚设了。
    """

    entity_type: Literal["product", "coupon", "order"]
    path: str


class EntityRequire(BaseModel):
    """写操作必须已被本会话读到过的实体参数（spec §8）。"""

    entity_type: Literal["product", "coupon", "order"]
    arg: str
    optional: bool = False
```

把 `ToolSpec` 改为：

```python
class ToolSpec(BaseModel):
    name: str
    kind: Literal["read", "write"]
    effects: list[EffectOp] = Field(default_factory=list)
    taint_source: bool = False
    taint_sink: bool = False
    # 参数契约（JSON Schema）。放在网关侧的工具清单里而不是工具自己的代码里，
    # 理由与效果声明相同（spec §4.3）：接入新工具只改一个文件，工具侧零改动。
    args_schema: dict[str, Any] = Field(default_factory=dict)
    # 执行成功后从返回值里登记哪些实体（provenance 的「发」）。
    emits: list[EntityEmit] = Field(default_factory=list)
    # 执行前必须已被本会话读到过哪些实体（provenance 的「收」）。
    requires: list[EntityRequire] = Field(default_factory=list)
```

- [x] **Step 4: 重写 `src/guardrail/tools/registry.py`**

```python
from __future__ import annotations

from typing import Any

from guardrail.models import (
    CouponSnapshot,
    EffectOp,
    EntityEmit,
    EntityRequire,
    OrderSnapshot,
    ProductSnapshot,
    ToolSpec,
)

_ENTITY_ID = {"type": "string", "minLength": 1}
_ORDER_ID = {"type": "string", "minLength": 1}

# 参数契约是网关侧数据。additionalProperties 一律 False：模型多报一个字段
# 通常意味着它在编造，而服务端派生的字段（absolute_delta_cents）绝不能
# 由模型自己塞进来。
_ARGS_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_products": {
        "type": "object",
        "properties": {"category": {"type": "string", "minLength": 1}},
        "additionalProperties": False,
    },
    "get_product": {
        "type": "object",
        "properties": {"product_id": _ENTITY_ID},
        "required": ["product_id"],
        "additionalProperties": False,
    },
    "get_order": {
        "type": "object",
        "properties": {"order_id": _ORDER_ID},
        "required": ["order_id"],
        "additionalProperties": False,
    },
    "update_price": {
        "type": "object",
        "properties": {
            "product_id": _ENTITY_ID,
            "delta_pct": {"type": "number"},
        },
        "required": ["product_id", "delta_pct"],
        "additionalProperties": False,
    },
    "update_stock": {
        "type": "object",
        "properties": {
            "product_id": _ENTITY_ID,
            "delta": {"type": "integer"},
        },
        "required": ["product_id", "delta"],
        "additionalProperties": False,
    },
    "create_coupon": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "minLength": 1, "maxLength": 64},
            "discount_pct": {
                "type": "number",
                "exclusiveMinimum": 0,
                "exclusiveMaximum": 100,
            },
            "max_uses": {"type": "integer", "minimum": 1},
        },
        "required": ["code", "discount_pct", "max_uses"],
        "additionalProperties": False,
    },
    "create_order": {
        "type": "object",
        "properties": {
            "product_id": _ENTITY_ID,
            "qty": {"type": "integer", "minimum": 1},
            # 券可带可不带，显式 null 也合法（handler 用 args.get 读它）。
            "coupon_id": {"type": ["string", "null"], "minLength": 1},
        },
        "required": ["product_id", "qty"],
        "additionalProperties": False,
    },
    "refund_order": {
        "type": "object",
        "properties": {"order_id": _ORDER_ID},
        "required": ["order_id"],
        "additionalProperties": False,
    },
    "send_email": {
        "type": "object",
        "properties": {
            "to": {"type": "string", "minLength": 3},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["to"],
        "additionalProperties": False,
    },
}

# 效果声明是网关侧数据，不是工具的代码。
# 工具实现（handlers.py）不含任何护栏逻辑；接入新工具时只改这里。
TOOL_SPECS: dict[str, ToolSpec] = {
    "list_products": ToolSpec(
        name="list_products",
        kind="read",
        args_schema=_ARGS_SCHEMAS["list_products"],
        emits=[EntityEmit(entity_type="product", path="products[].id")],
    ),
    "get_product": ToolSpec(
        name="get_product",
        kind="read",
        args_schema=_ARGS_SCHEMAS["get_product"],
        emits=[EntityEmit(entity_type="product", path="product.id")],
    ),
    "get_order": ToolSpec(
        name="get_order",
        kind="read",
        taint_source=True,
        args_schema=_ARGS_SCHEMAS["get_order"],
        emits=[EntityEmit(entity_type="order", path="order.id")],
    ),
    "update_price": ToolSpec(
        name="update_price",
        kind="write",
        args_schema=_ARGS_SCHEMAS["update_price"],
        effects=[
            EffectOp(
                target="product:{args.product_id}",
                field="list_price_cents",
                op="add",
                value="{args.absolute_delta_cents}",
            )
        ],
        emits=[EntityEmit(entity_type="product", path="after.id")],
        requires=[EntityRequire(entity_type="product", arg="product_id")],
    ),
    "update_stock": ToolSpec(
        name="update_stock",
        kind="write",
        args_schema=_ARGS_SCHEMAS["update_stock"],
        effects=[
            EffectOp(
                target="product:{args.product_id}",
                field="stock",
                op="add",
                value="{args.delta}",
            )
        ],
        emits=[EntityEmit(entity_type="product", path="after.id")],
        requires=[EntityRequire(entity_type="product", arg="product_id")],
    ),
    "create_coupon": ToolSpec(
        name="create_coupon",
        kind="write",
        args_schema=_ARGS_SCHEMAS["create_coupon"],
        effects=[
            EffectOp(
                target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}"
            )
        ],
        emits=[EntityEmit(entity_type="coupon", path="coupon_id")],
    ),
    "create_order": ToolSpec(
        name="create_order",
        kind="write",
        args_schema=_ARGS_SCHEMAS["create_order"],
        effects=[
            EffectOp(
                target="order:{args.order_id}", field="", op="append", value="{args.order_id}"
            ),
            # 真实商城（shop/store.py create_order）只做两件事：INSERT 订单，以及
            # 「带券时」UPDATE coupons SET used = used + 1。它不改 products.stock，
            # 也不在无券下单时碰任何券。optional=True 让 coupon_id 为 None 时这条
            # 效果退化为无操作，与商城逐字对齐。
            EffectOp(
                target="coupon:{args.coupon_id}",
                field="used",
                op="add",
                value=1,
                optional=True,
            ),
        ],
        emits=[
            EntityEmit(entity_type="order", path="order_id"),
            # 商品 id 是 provenance 门真正要看的东西：它藏在 order 载荷里，
            # 不在效果声明的 target 上，所以 requires 里必须显式再写一次。
            EntityEmit(entity_type="product", path="order.product_id"),
        ],
        requires=[
            EntityRequire(entity_type="product", arg="product_id"),
            EntityRequire(entity_type="coupon", arg="coupon_id", optional=True),
        ],
    ),
    "refund_order": ToolSpec(
        name="refund_order",
        kind="write",
        args_schema=_ARGS_SCHEMAS["refund_order"],
        effects=[
            EffectOp(target="order:{args.order_id}", field="status", op="set", value="refunded")
        ],
        emits=[EntityEmit(entity_type="order", path="order_id")],
        requires=[EntityRequire(entity_type="order", arg="order_id")],
    ),
    "send_email": ToolSpec(
        name="send_email",
        kind="write",
        taint_sink=True,
        args_schema=_ARGS_SCHEMAS["send_email"],
    ),
}

_VALID_OPS = {"add", "set", "mul", "append"}

# 实体类型 → 快照模型，用于校验 effect 的 field 是否真的存在于对应模型上。
_ENTITY_SNAPSHOTS: dict[str, type] = {
    "product": ProductSnapshot,
    "coupon": CouponSnapshot,
    "order": OrderSnapshot,
}

# append 只能新建 coupon / order；projection.py 会拒绝 product 上的 append。
_APPENDABLE_ENTITY_TYPES = {"coupon", "order"}

_PATH_HEAD = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _check_effects(name: str, spec: ToolSpec) -> None:
    for op in spec.effects:
        if op.op not in _VALID_OPS:
            raise RuntimeError(f"{name} 含非法 op: {op.op}")
        if ":" not in op.target:
            raise RuntimeError(f"{name} 的 target 缺少实体前缀: {op.target}")
        entity_type, _, entity_id = op.target.partition(":")
        if entity_type not in _ENTITY_SNAPSHOTS:
            raise RuntimeError(f"{name} 的 target 实体类型未知: {op.target}")
        if not entity_id:
            raise RuntimeError(f"{name} 的 target 实体 id 为空: {op.target}")
        if op.field and op.field not in _ENTITY_SNAPSHOTS[entity_type].model_fields:
            raise RuntimeError(
                f"{name} 的字段 {op.field!r} 不在 {entity_type} 快照模型上: {op.target}"
            )
        if op.op == "append" and entity_type not in _APPENDABLE_ENTITY_TYPES:
            raise RuntimeError(f"{name} 的 append 仅支持 coupon / order，收到 {op.target}")


def _check_args_schema(name: str, spec: ToolSpec) -> None:
    schema = spec.args_schema
    if not schema:
        raise RuntimeError(f"{name} 未声明 args_schema")
    if schema.get("type") != "object":
        raise RuntimeError(f"{name} 的 args_schema.type 必须是 object")
    # additionalProperties 不为 False 等于允许模型注入服务端派生字段，
    # 这是一个安全属性而不是风格偏好，所以放进启动期自检。
    if schema.get("additionalProperties") is not False:
        raise RuntimeError(f"{name} 的 args_schema 必须设置 additionalProperties: false")
    for required in schema.get("required", []):
        if required not in schema.get("properties", {}):
            raise RuntimeError(f"{name} 的 required 字段 {required!r} 未在 properties 中声明")


def _check_entity_refs(name: str, spec: ToolSpec) -> None:
    schema_required = set(spec.args_schema.get("required", []))
    for emit in spec.emits:
        head = emit.path.removesuffix("[]").split(".", 1)[0]
        if not head or head[0] not in _PATH_HEAD:
            raise RuntimeError(f"{name} 的 emits 路径非法: {emit.path}")
    for require in spec.requires:
        if require.arg not in spec.args_schema.get("properties", {}):
            raise RuntimeError(
                f"{name} 的 requires 引用了未声明的参数 {require.arg!r}（不在 args_schema 里）"
            )
        # 必填参数不可能是「可选依赖」：模型一定会下发它，provenance 就一定会
        # 被检查。标成 optional 的只该是那些 schema 里非必填的参数（如 coupon_id）。
        if require.optional and require.arg in schema_required:
            raise RuntimeError(
                f"{name} 的 requires 字段 {require.arg!r} 是必填参数，不能标为 optional"
            )


def assert_specs_valid(specs: dict[str, ToolSpec] | None = None) -> None:
    """启动期自检。工具清单有问题时进程应当直接起不来。"""
    if specs is None:
        specs = TOOL_SPECS
    if len(specs) != 9:
        raise RuntimeError(f"工具数量应为 9，实际 {len(specs)}")
    for name, spec in specs.items():
        if spec.name != name:
            raise RuntimeError(f"工具键名与 spec.name 不一致: {name} != {spec.name}")
        # 效果先校验：它承载的是「这个工具会改什么」，错了会直接让投影失真；
        # 参数契约与实体引用排在其后。
        _check_effects(name, spec)
        _check_args_schema(name, spec)
        _check_entity_refs(name, spec)
```

- [x] **Step 5: 写 `src/guardrail/tools/contracts.py`**

```python
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
    raise ArgsValidationError(
        f"工具 {spec.name!r} 的参数 {location} 不合法：{first.message}"
    )
```

- [x] **Step 6: 修 `tests/test_tool_registry.py` 的 `_specs_with_effect`**

现在的实现用 `ToolSpec(name=..., kind=..., effects=[effect])` 造一个裸 spec，它没有 `args_schema`，会先撞上新加的自检而报出与测试期望不符的消息。改为基于真实 spec 派生：

```python
def _specs_with_effect(effect: EffectOp) -> dict[str, ToolSpec]:
    """在真实 update_price spec 的基础上换掉效果声明。

    必须继承真实 spec 的 args_schema / emits / requires，否则自检会先在参数
    契约上报错，测试就测不到效果声明那一层了。
    """
    specs = dict(TOOL_SPECS)
    base = TOOL_SPECS["update_price"]
    specs["update_price"] = base.model_copy(update={"effects": [effect]})
    return specs
```

- [x] **Step 7: 跑测试确认通过**

Run: `uv run pytest tests/test_tool_contracts.py tests/test_tool_registry.py -v`
Expected: 26 passed（contracts 18 + registry 11，其中 3 条参数化展开）

- [x] **Step 8: Commit**

```bash
git add src/guardrail/models.py src/guardrail/tools/registry.py \
  src/guardrail/tools/contracts.py tests/test_tool_contracts.py tests/test_tool_registry.py
git commit -m "feat: 工具参数契约与实体引用声明，参数契约与效果声明同处网关侧清单"
```

---

### Task 4: 策略模型、加载与启动期 lint

**Files:**
- Create: `policies/single_call.yaml`
- Create: `src/guardrail/policy/single.py`
- Create: `src/guardrail/policy/loader.py`
- Create: `src/guardrail/policy/lint.py`
- Test: `tests/test_policy_load.py`

**Interfaces:**
- Consumes: `ToolSpec` / `TOOL_SPECS`（Task 3）、`arg_names` / `ExpressionError`（Task 2）
- Produces:
  - `guardrail.policy.single.PolicyError`
  - `guardrail.policy.single.ToolMatch(tool: str)`
  - `guardrail.policy.single.SingleRule(id, match, message, deny_if=None, cap_field=None, max=None, compute_from=None)`
  - `guardrail.policy.single.SingleCallPolicy(version, permissions, rules)`，方法 `rules_for(tool) -> list[SingleRule]`
  - `guardrail.policy.lint.CAP_FIELDS: tuple[str, ...]`
  - `guardrail.policy.lint.lint_policy(policy, tools) -> None`
  - `guardrail.policy.loader.load_policy(path: str) -> SingleCallPolicy`

- [x] **Step 1: 写 `policies/single_call.yaml`**

```yaml
# 单次策略（spec §6）。策略即数据：网关启动时加载并自检，缺文件 / 解析失败 /
# 自检不过一律拒绝启动（spec §10.2 fail-closed）。
#
# 规则有两种形态，互斥：
#   语法层  deny_if                          —— 读参数即可判定，不需要投影。
#   结果层  cap_field + max + compute_from   —— 从投影结果态读值，不信 args。
version: 1

# 权限表（spec §6.1 第 5 步）。**未列出的 agent 没有任何工具权限**：
# 新增 agent 必须显式授权，不会自动继承全量。这比「默认放行」重要得多——
# 默认放行意味着漏配一次就等于交付了一个越权 Agent。
permissions:
  # 定价 Agent：只管价格与库存。spec §13 场景 4 的 pricing_agent。
  pricing_agent:
    - list_products
    - get_product
    - update_price
    - update_stock
  # 营销 Agent：发券、下单，外加邮件出口（污点汇，spec §3.4③）。
  marketing_agent:
    - list_products
    - get_product
    - create_coupon
    - create_order
    - send_email
  # 运营：全量。demo 里的普通会话用它。
  ops_agent:
    - list_products
    - get_product
    - get_order
    - update_price
    - update_stock
    - create_coupon
    - create_order
    - refund_order
    - send_email
  # 风控观察员：只读。
  risk_auditor:
    - list_products
    - get_product
    - get_order

rules:
  # 语法层：单次降价上限。spec §13 场景 1 的判定依据。
  - id: max_single_price_cut
    match: {tool: update_price}
    deny_if: "abs(args.delta_pct) > 10"
    message: "单次降价不得超过 10%"

  # 语法层：单次库存调整上限。防止一次 -999999 把库存打穿。
  - id: max_single_stock_delta
    match: {tool: update_stock}
    deny_if: "abs(args.delta) > 500"
    message: "单次库存调整不得超过 500 件"

  # 语法层：券折扣率上限。
  # 阈值取 80 而不是更严的数字，是**给组合风险留出可触发的区间**：序列型规则
  # （spec §3.4②「发一张 ≥50% 的券再自己下单核销」）需要 50~80 区间的券能真的
  # 发出去，否则计划 ③ 的场景永远触发不了。两层策略之间必须留缝——
  # 这也是为什么「阈值」不能拍脑袋，得看清它会挡掉谁。
  - id: max_coupon_discount
    match: {tool: create_coupon}
    deny_if: "args.discount_pct > 80"
    message: "单张优惠券折扣不得超过 80%"

  # 语法层：带券订单的数量上限。顺带覆盖 args 可选参数的表达式求值路径。
  - id: no_discounted_bulk_order
    match: {tool: create_order}
    deny_if: "args.coupon_id != null and args.qty >= 50"
    message: "带券订单数量不得超过 49 件"

  # 结果层：单笔订单金额上限。金额从**投影后的订单终态**读（unit_price × qty），
  # 不读 args——模型可以谎报 args 里的任何数字，谎报不了投影结果（spec §6.3）。
  # 单位是整数分，与全局约束一致。50000 分 = 500.00 元。
  - id: cap_order_amount
    match: {tool: create_order}
    cap_field: total_amount_cents
    max: 50000
    compute_from: resulting_state
    message: "单笔订单金额不得超过 500.00 元"
```

- [x] **Step 2: 写失败测试 `tests/test_policy_load.py`**

```python
import textwrap

import pytest

from guardrail.models import EffectOp, ToolSpec
from guardrail.policy.loader import load_policy
from guardrail.policy.lint import CAP_FIELDS, lint_policy
from guardrail.policy.single import PolicyError, SingleCallPolicy
from guardrail.tools.registry import TOOL_SPECS

REPO_POLICY = "policies/single_call.yaml"


def _write(tmp_path, text: str) -> str:
    path = tmp_path / "single_call.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


# ---------- 仓库里那份策略本身 ----------

def test_shipped_policy_loads_and_lints():
    policy = load_policy(REPO_POLICY)
    assert policy.version == 1
    lint_policy(policy, TOOL_SPECS)


def test_shipped_policy_has_no_rule_on_unknown_tool():
    policy = load_policy(REPO_POLICY)
    for rule in policy.rules:
        assert rule.match.tool in TOOL_SPECS


def test_shipped_policy_keeps_room_for_combined_risk():
    # 序列型组合风险（计划 ③）需要能发出一张 50~80% 的券。
    # 语法层把折扣卡在 80 就是这个目的：两层之间必须留缝。
    policy = load_policy(REPO_POLICY)
    thresholds = [
        r for r in policy.rules_for("create_coupon") if r.deny_if is not None
    ]
    assert thresholds, "create_coupon 必须留一条语法层阈值之外的可发券区间"
    assert all("80" in (r.deny_if or "") for r in thresholds)


def test_cap_fields_are_declared():
    assert "total_amount_cents" in CAP_FIELDS


# ---------- 加载 ----------

def test_missing_file_raises(tmp_path):
    with pytest.raises(PolicyError, match="不存在"):
        load_policy(str(tmp_path / "nope.yaml"))


def test_unparsable_yaml_raises(tmp_path):
    with pytest.raises(PolicyError, match="无法解析"):
        load_policy(_write(tmp_path, "rules: [\n  - id: x\n bad indent"))


def test_non_mapping_root_raises(tmp_path):
    with pytest.raises(PolicyError, match="顶层必须是映射"):
        load_policy(_write(tmp_path, "- a\n- b\n"))


def test_rule_with_both_forms_raises(tmp_path):
    with pytest.raises(PolicyError, match="结构非法"):
        load_policy(
            _write(
                tmp_path,
                """
                rules:
                  - id: both
                    match: {tool: update_price}
                    deny_if: "abs(args.delta_pct) > 10"
                    cap_field: total_amount_cents
                    max: 100
                    compute_from: resulting_state
                    message: x
                """,
            )
        )


def test_rule_with_no_form_raises(tmp_path):
    with pytest.raises(PolicyError, match="结构非法"):
        load_policy(
            _write(
                tmp_path,
                """
                rules:
                  - id: neither
                    match: {tool: update_price}
                    message: x
                """,
            )
        )


def test_result_form_without_max_raises(tmp_path):
    with pytest.raises(PolicyError, match="结构非法"):
        load_policy(
            _write(
                tmp_path,
                """
                rules:
                  - id: half
                    match: {tool: create_order}
                    cap_field: total_amount_cents
                    compute_from: resulting_state
                    message: x
                """,
            )
        )


# ---------- lint ----------

def _all_tools_permissions() -> str:
    """授权全部 9 个工具，让每条 lint 用例只触发它想测的那一个问题。

    否则「没有任何 agent 授权」会混进每一条错误信息里——测试仍然能通过
    （用的是子串匹配），但报错会指向一个与被测代码无关的原因。
    """
    return "  a: [" + ", ".join(sorted(TOOL_SPECS)) + "]"


def _policy(rules_yaml: str, permissions_yaml: str | None = None) -> SingleCallPolicy:
    import yaml

    return SingleCallPolicy.model_validate(
        yaml.safe_load(
            textwrap.dedent(
                f"""
                version: 1
                permissions:
                {permissions_yaml or _all_tools_permissions()}
                rules:
                {rules_yaml}
                """
            )
        )
    )


def test_lint_rejects_unknown_tool_in_rule():
    p = _policy(
        """
          - id: r
            match: {tool: teleport}
            deny_if: "args.x > 1"
            message: m
        """
    )
    with pytest.raises(PolicyError, match="未知的工具"):
        lint_policy(p, TOOL_SPECS)


def test_lint_rejects_duplicate_rule_id():
    p = _policy(
        """
          - id: dup
            match: {tool: update_price}
            deny_if: "args.delta_pct > 1"
            message: m
          - id: dup
            match: {tool: update_stock}
            deny_if: "args.delta > 1"
            message: m
        """
    )
    with pytest.raises(PolicyError, match="重复"):
        lint_policy(p, TOOL_SPECS)


def test_lint_rejects_typo_in_expression_arg():
    # args.delt_pct —— 拼错了。没有这道检查，规则会静默地永不触发。
    p = _policy(
        """
          - id: typo
            match: {tool: update_price}
            deny_if: "abs(args.delt_pct) > 10"
            message: m
        """
    )
    with pytest.raises(PolicyError, match="delt_pct"):
        lint_policy(p, TOOL_SPECS)


def test_lint_rejects_undeclared_optional_arg():
    p = _policy(
        """
          - id: nope
            match: {tool: create_order}
            deny_if: "args.coupon > 1"
            message: m
        """
    )
    with pytest.raises(PolicyError, match="coupon"):
        lint_policy(p, TOOL_SPECS)


def test_lint_rejects_invalid_expression():
    p = _policy(
        """
          - id: bad
            match: {tool: update_price}
            deny_if: "args.__class__ > 1"
            message: m
        """
    )
    with pytest.raises(PolicyError, match="表达式"):
        lint_policy(p, TOOL_SPECS)


def test_lint_rejects_unknown_cap_field():
    p = _policy(
        """
          - id: cap
            match: {tool: create_order}
            cap_field: total_amount_yuan
            max: 100
            compute_from: resulting_state
            message: m
        """
    )
    with pytest.raises(PolicyError, match="total_amount_yuan"):
        lint_policy(p, TOOL_SPECS)


def test_lint_rejects_unknown_tool_in_permissions():
    p = _policy(
        """
          - id: r
            match: {tool: update_price}
            deny_if: "args.delta_pct > 1"
            message: m
        """,
        permissions_yaml="  a: [list_products, teleport]",
    )
    with pytest.raises(PolicyError, match="teleport"):
        lint_policy(p, TOOL_SPECS)


def test_lint_rejects_empty_permission_list():
    p = _policy(
        """
          - id: r
            match: {tool: update_price}
            deny_if: "args.delta_pct > 1"
            message: m
        """,
        permissions_yaml="  a: []",
    )
    with pytest.raises(PolicyError, match="空列表"):
        lint_policy(p, TOOL_SPECS)


def test_lint_rejects_ungranted_tool():
    # list_products 若没有任何 agent 授权，多半是有人漏配了，而不是有意下线。
    tools = {k: v for k, v in TOOL_SPECS.items() if k != "get_order"}
    p = _policy(
        """
          - id: r
            match: {tool: update_price}
            deny_if: "args.delta_pct > 1"
            message: m
        """,
        permissions_yaml="  a: [list_products, get_product, update_price, update_stock]",
    )
    with pytest.raises(PolicyError, match="没有任何 agent 授权"):
        lint_policy(p, tools)


def test_lint_collects_all_problems_at_once():
    # 一次报全部问题，而不是修一个发现一个——启动期报错的价值就在这。
    p = _policy(
        """
          - id: a
            match: {tool: teleport}
            deny_if: "args.nope > 1"
            message: m
        """
    )
    with pytest.raises(PolicyError) as exc:
        lint_policy(p, TOOL_SPECS)
    text = str(exc.value)
    assert "teleport" in text
    assert "nope" in text


def test_policy_repr_is_readable():
    p = load_policy(REPO_POLICY)
    assert "max_single_price_cut" in repr(p)


def test_shipped_policy_effect_free_of_hardcoded_cents():
    # 结果层上限的单位是整数分：50000 分 = 500.00 元，不是 5 万元。
    p = load_policy(REPO_POLICY)
    caps = [r for r in p.rules if r.cap_field is not None]
    assert [r.cap_field for r in caps] == ["total_amount_cents"]
    assert caps[0].max == 50000


def test_tool_specs_still_valid_after_registry_change():
    from guardrail.tools.registry import assert_specs_valid

    assert_specs_valid()
    assert isinstance(TOOL_SPECS["update_price"].effects[0], EffectOp)
    assert isinstance(TOOL_SPECS["update_price"], ToolSpec)
```

- [x] **Step 3: 跑测试确认失败**

Run: `uv run pytest tests/test_policy_load.py -v`
Expected: FAIL —— `FileNotFoundError`（`policies/single_call.yaml` 还没建）且 `guardrail.policy.loader` 不存在

- [x] **Step 4: 写 `src/guardrail/policy/single.py`**

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PolicyError(Exception):
    """策略缺失、无法解析或不通过自检。

    进程应当拒绝启动（spec §10.2）：一个没加载上策略的网关不是「宽松的网关」，
    是一个不知道自己在干什么的网关。
    """


class ToolMatch(BaseModel):
    tool: str


class SingleRule(BaseModel):
    """一条单次策略规则。

    两种形态互斥：

    - **语法层**：只给 `deny_if`，读参数即可判定（spec §6.1 第 6 步）。
    - **结果层**：给 `cap_field` + `max` + `compute_from`，从投影结果态读值
      （spec §6.3）。

    刻意不做「两者都声明」或「两者都不声明」——那两种配置都会让规则的真实
    语义变得 DependsOn 解析顺序，所以直接在模型层拒绝。
    """

    id: str
    match: ToolMatch
    message: str
    deny_if: str | None = None
    cap_field: str | None = None
    max: int | None = None
    compute_from: Literal["resulting_state"] | None = None

    @model_validator(mode="after")
    def _exactly_one_form(self) -> "SingleRule":
        has_syntax = self.deny_if is not None
        has_result = self.cap_field is not None
        if has_syntax == has_result:
            raise ValueError(
                f"规则 {self.id!r} 必须且只能声明 deny_if 或 cap_field 之一"
            )
        if has_result:
            if self.max is None or self.compute_from is None:
                raise ValueError(
                    f"规则 {self.id!r} 的结果层形态必须同时声明 max 与 compute_from"
                )
        elif self.max is not None or self.compute_from is not None:
            raise ValueError(
                f"规则 {self.id!r} 的语法层形态不应声明 max / compute_from"
            )
        return self


class SingleCallPolicy(BaseModel):
    version: int = 1
    permissions: dict[str, list[str]] = Field(default_factory=dict)
    rules: list[SingleRule] = Field(default_factory=list)

    def rules_for(self, tool: str) -> list[SingleRule]:
        return [rule for rule in self.rules if rule.match.tool == tool]

    def is_permitted(self, agent_id: str, tool: str) -> bool:
        """未列出的 agent 没有任何权限。

        这是刻意的 fail-closed：新增一个 agent 必须显式授权，而不是继承全量。
        反过来（默认全放行）意味着任何一次漏配都直接等价于越权。
        """
        return tool in self.permissions.get(agent_id, [])
```

- [x] **Step 5: 写 `src/guardrail/policy/lint.py`**

```python
from __future__ import annotations

from guardrail.models import ToolSpec
from guardrail.policy.expr import ExpressionError, arg_names
from guardrail.policy.single import PolicyError, SingleCallPolicy

# 结果层可读字段的闭集。新增一个字段必须在这里同时给出「从哪个快照、怎么算」，
# 而不是让策略文件自己写表达式——那等于把求值沙箱开一个口子。
CAP_FIELDS: tuple[str, ...] = ("total_amount_cents",)


def lint_policy(policy: SingleCallPolicy, tools: dict[str, ToolSpec]) -> None:
    """启动期策略自检。一次性报出全部问题。

    逐个报错、逐个修的体验在启动期是灾难：改一个 YAML 要重启三次进程。
    """
    problems: list[str] = []

    seen: set[str] = set()
    for rule in policy.rules:
        if rule.id in seen:
            problems.append(f"规则 id 重复：{rule.id}")
        seen.add(rule.id)

        spec = tools.get(rule.match.tool)
        if spec is None:
            problems.append(f"规则 {rule.id!r} 引用了未知的工具：{rule.match.tool}")
            continue

        if rule.deny_if is not None:
            problems.extend(_lint_expression(rule.id, rule.deny_if, spec))
        elif rule.cap_field is not None and rule.cap_field not in CAP_FIELDS:
            problems.append(
                f"规则 {rule.id!r} 的 cap_field {rule.cap_field!r} 不在可读字段集 "
                f"{list(CAP_FIELDS)} 内"
            )

    problems.extend(_lint_permissions(policy, tools))
    problems.extend(_lint_tool_coverage(policy, tools))

    if problems:
        detail = "\n".join(f"  - {p}" for p in problems)
        raise PolicyError(f"策略文件未通过自检：\n{detail}")


def _lint_expression(rule_id: str, expression: str, spec: ToolSpec) -> list[str]:
    try:
        names = arg_names(expression)
    except ExpressionError as exc:
        return [f"规则 {rule_id!r} 的表达式非法：{exc}"]

    declared = set(spec.args_schema.get("properties", {}))
    unknown = sorted(names - declared)
    if unknown:
        return [
            f"规则 {rule_id!r} 的表达式引用了 {spec.name} 未声明的参数：{unknown}"
        ]
    return []


def _lint_permissions(policy: SingleCallPolicy, tools: dict[str, ToolSpec]) -> list[str]:
    problems: list[str] = []
    for agent_id, granted in policy.permissions.items():
        if not granted:
            problems.append(f"agent {agent_id!r} 的权限列表是空列表")
        for tool in granted:
            if tool not in tools:
                problems.append(f"agent {agent_id!r} 被授予了未知的工具：{tool}")
    return problems


def _lint_tool_coverage(policy: SingleCallPolicy, tools: dict[str, ToolSpec]) -> list[str]:
    granted = {tool for tool_list in policy.permissions.values() for tool in tool_list}
    ungranted = sorted(set(tools) - granted)
    if ungranted:
        return [f"以下工具没有任何 agent 授权：{ungranted}"]
    return []
```

- [x] **Step 6: 写 `src/guardrail/policy/loader.py`**

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from guardrail.policy.lint import lint_policy
from guardrail.policy.single import PolicyError, SingleCallPolicy
from guardrail.tools.registry import TOOL_SPECS


def load_policy(path: str) -> SingleCallPolicy:
    """加载并自检单次策略。任何环节失败都抛 PolicyError，调用方必须拒绝启动。"""
    file = Path(path)
    if not file.is_file():
        raise PolicyError(f"策略文件不存在：{path}")

    try:
        raw: Any = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyError(f"策略文件无法解析：{exc}") from exc

    if not isinstance(raw, dict):
        raise PolicyError(
            f"策略文件顶层必须是映射，收到 {type(raw).__name__}"
        )

    try:
        policy = SingleCallPolicy.model_validate(raw)
    except ValidationError as exc:
        raise PolicyError(f"策略文件结构非法：{exc}") from exc

    lint_policy(policy, TOOL_SPECS)
    return policy
```

- [x] **Step 7: 跑测试确认通过**

Run: `uv run pytest tests/test_policy_load.py -v`
Expected: 27 passed

- [x] **Step 8: Commit**

```bash
git add policies/single_call.yaml src/guardrail/policy/single.py \
  src/guardrail/policy/loader.py src/guardrail/policy/lint.py tests/test_policy_load.py
git commit -m "feat: 单次策略模型、YAML 加载与启动期自检；语法层阈值给组合风险留出可触发区间"
```

---

### Task 5: Provenance 门

**Files:**
- Create: `src/guardrail/provenance.py`
- Create: `src/guardrail/stores/provenance.py`
- Test: `tests/test_provenance.py`

**Interfaces:**
- Consumes: `ToolSpec.emits` / `ToolSpec.requires`（Task 3）、`SqliteBackend`（Task 1）、`now_iso`（Task 1）
- Produces:
  - `guardrail.provenance.ProvenanceStore`（Protocol）：`async register(session_id, refs) -> None`、`async contains(session_id, entity_type, entity_id) -> bool`
  - `guardrail.provenance.emitted_entity_ids(spec: ToolSpec, result: dict) -> list[tuple[str, str]]`
  - `guardrail.provenance.register_result(store, session_id, spec, result) -> None`
  - `guardrail.provenance.check_requirements(store, session_id, spec, args) -> str | None`（返回拒绝原因或 `None`）
  - `guardrail.stores.provenance.SqliteProvenanceStore(backend)`

- [x] **Step 1: 写失败测试 `tests/test_provenance.py`**

```python
import pytest

from guardrail.models import EntityEmit, EntityRequire, ToolSpec
from guardrail.provenance import (
    check_requirements,
    emitted_entity_ids,
    register_result,
)
from guardrail.stores.provenance import SqliteProvenanceStore
from guardrail.stores.sqlite import SqliteBackend
from guardrail.tools.registry import TOOL_SPECS


@pytest.fixture
async def store(tmp_path):
    backend = SqliteBackend(str(tmp_path / "gateway.db"))
    await backend.connect()
    yield SqliteProvenanceStore(backend)
    await backend.close()


def _spec(**kwargs) -> ToolSpec:
    base = {"name": "t", "kind": "write", "args_schema": {"type": "object", "properties": {}}}
    return ToolSpec(**{**base, **kwargs})


# ---------- 路径解析 ----------

def test_emits_reads_nested_key():
    spec = _spec(emits=[EntityEmit(entity_type="product", path="product.id")])
    assert emitted_entity_ids(spec, {"product": {"id": "p-1"}}) == [("product", "p-1")]


def test_emits_reads_top_level_scalar():
    spec = _spec(emits=[EntityEmit(entity_type="coupon", path="coupon_id")])
    assert emitted_entity_ids(spec, {"coupon_id": "c-1"}) == [("coupon", "c-1")]


def test_emits_reads_list_wildcard():
    spec = _spec(emits=[EntityEmit(entity_type="product", path="products[].id")])
    result = {"products": [{"id": "p-1"}, {"id": "p-2"}, {"id": "p-3"}]}
    assert emitted_entity_ids(spec, result) == [
        ("product", "p-1"),
        ("product", "p-2"),
        ("product", "p-3"),
    ]


def test_emits_tolerates_missing_path():
    spec = _spec(emits=[EntityEmit(entity_type="product", path="product.id")])
    assert emitted_entity_ids(spec, {"order": {}}) == []


def test_emits_drops_non_string_ids():
    spec = _spec(emits=[EntityEmit(entity_type="product", path="product.id")])
    assert emitted_entity_ids(spec, {"product": {"id": 42}}) == []


def test_emits_drops_empty_string_ids():
    spec = _spec(emits=[EntityEmit(entity_type="product", path="product.id")])
    assert emitted_entity_ids(spec, {"product": {"id": ""}}) == []


def test_emits_supports_multiple_refs():
    spec = TOOL_SPECS["create_order"]
    ids = emitted_entity_ids(spec, {"order_id": "o-1", "order": {"product_id": "p-1"}})
    assert set(ids) == {("order", "o-1"), ("product", "p-1")}


# ---------- 登记 ----------

async def test_register_result_then_contains(store):
    spec = TOOL_SPECS["get_product"]
    await register_result(store, "s-1", spec, {"product": {"id": "p-1"}})
    assert await store.contains("s-1", "product", "p-1")


async def test_provenance_is_session_scoped(store):
    spec = TOOL_SPECS["get_product"]
    await register_result(store, "s-1", spec, {"product": {"id": "p-1"}})
    assert not await store.contains("s-2", "product", "p-1")


async def test_register_is_idempotent(store):
    spec = TOOL_SPECS["get_product"]
    for _ in range(3):
        await register_result(store, "s-1", spec, {"product": {"id": "p-1"}})
    rows = await store.backend.fetchall("SELECT * FROM provenance")
    assert len(rows) == 1


# ---------- 校验 ----------

async def test_requirement_satisfied_after_read(store):
    spec = TOOL_SPECS["update_price"]
    assert check_requirements(store, "s-1", spec, {"product_id": "p-1"}) is not None
    await register_result(store, "s-1", TOOL_SPECS["get_product"], {"product": {"id": "p-1"}})
    assert check_requirements(store, "s-1", spec, {"product_id": "p-1"}) is None


async def test_requirement_names_the_offending_entity(store):
    reason = check_requirements(
        store, "s-1", TOOL_SPECS["update_price"], {"product_id": "p-evil"}
    )
    assert "product" in reason
    assert "p-evil" in reason
    assert "s-1" in reason


async def test_entity_from_another_session_is_rejected(store):
    await register_result(store, "s-1", TOOL_SPECS["get_product"], {"product": {"id": "p-1"}})
    assert check_requirements(store, "s-2", TOOL_SPECS["update_price"], {"product_id": "p-1"})


async def test_optional_requirement_absent_is_fine(store):
    spec = TOOL_SPECS["create_order"]
    await register_result(store, "s-1", TOOL_SPECS["get_product"], {"product": {"id": "p-1"}})
    assert check_requirements(store, "s-1", spec, {"product_id": "p-1"}) is None


async def test_optional_requirement_present_must_be_known(store):
    spec = TOOL_SPECS["create_order"]
    await register_result(store, "s-1", TOOL_SPECS["get_product"], {"product": {"id": "p-1"}})
    reason = check_requirements(
        store, "s-1", spec, {"product_id": "p-1", "coupon_id": "c-forged"}
    )
    assert reason is not None
    assert "c-forged" in reason


async def test_read_tool_has_no_requirements_to_check(store):
    assert check_requirements(store, "s-1", TOOL_SPECS["get_order"], {"order_id": "o-9"}) is None


async def test_write_tool_without_requirements_passes(store):
    # create_coupon 不引用任何已有实体——它创造新东西，不需要 provenance。
    assert check_requirements(store, "s-1", TOOL_SPECS["create_coupon"], {"code": "X"}) is None
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_provenance.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'guardrail.provenance'`

- [x] **Step 3: 写 `src/guardrail/provenance.py`**

```python
from __future__ import annotations

from typing import Any, Protocol

from guardrail.clock import now_iso
from guardrail.models import ToolSpec


class ProvenanceStore(Protocol):
    """Provenance 登记存储（spec §8）。

    对抗的是间接提示注入：被污染的模型无法凭空编造一个真实存在的商品 id
    去改价，因为它必须先在本会话读到过。
    """

    async def register(
        self, session_id: str, refs: list[tuple[str, str]]
    ) -> None: ...

    async def contains(self, session_id: str, entity_type: str, entity_id: str) -> bool: ...


def _children(node: Any, key: str) -> list[Any]:
    """从一个 JSON 节点里取出下一层候选值。

    `key` 为空表示不再下降，当前节点本身就是候选。列表会被摊平——`[]` 通配
    就是靠这一步生效的。
    """
    if key == "":
        return [node]
    if isinstance(node, list):
        return list(node)
    if not isinstance(node, dict) or key not in node:
        return []
    value = node[key]
    return list(value) if isinstance(value, list) else [value]


def _resolve(node: Any, path: str) -> list[str]:
    """按受限路径语法取出实体 id 列表。

    路径不存在时返回空列表而不是抛错：工具换了返回结构时，provenance 少登记
    几个实体会让写操作被拒（fail-closed 的方向），不会让它凭空放行。
    """
    head, sep, tail = path.partition(".")
    if head.endswith("[]"):
        head = head[:-2]
        children = _children(node, head)
        return [
            found for child in children for found in _resolve(child, tail if sep else "")
        ]
    children = _children(node, head)
    if sep:
        return [found for child in children for found in _resolve(child, tail)]
    return children


def emitted_entity_ids(spec: ToolSpec, result: dict[str, Any]) -> list[tuple[str, str]]:
    """从工具返回值里取出它产出的 (实体类型, 实体 id) 列表。"""
    refs: list[tuple[str, str]] = []
    for emit in spec.emits:
        for value in _resolve(result, emit.path):
            if isinstance(value, str) and value:
                refs.append((emit.entity_type, value))
    return refs


async def register_result(
    store: ProvenanceStore,
    session_id: str,
    spec: ToolSpec,
    result: dict[str, Any],
) -> None:
    """执行成功后登记本次调用产出的实体。"""
    refs = emitted_entity_ids(spec, result)
    if refs:
        await store.register(session_id, refs)


async def check_requirements(
    store: ProvenanceStore,
    session_id: str,
    spec: ToolSpec,
    args: dict[str, Any],
) -> str | None:
    """校验写操作引用的实体是否都已在本会话出现过。返回拒绝原因或 None。"""
    for require in spec.requires:
        value = args.get(require.arg)
        if value is None or value == "":
            # 必填依赖缺参数，交给参数契约（第 4 步）报错，那条消息更具体。
            if require.optional:
                continue
            continue
        if not isinstance(value, str):
            continue
        if await store.contains(session_id, require.entity_type, value):
            continue
        return (
            f"Provenance 拒绝：实体 {require.entity_type}:{value!r} 未在会话 {session_id} "
            f"中出现过。写操作只能作用于本会话工具返回过的实体。"
        )
    return None
```

> **`check_requirements` 里两处 `continue` 是有意的**：必填依赖缺参数时不在这里报错，而是让第 4 步的参数契约去报——那条消息会点名工具和参数，对 Agent 更可读。这里重复报一次只会让拒绝原因变长。

- [x] **Step 4: 写 `src/guardrail/stores/provenance.py`**

```python
from __future__ import annotations

from guardrail.clock import now_iso
from guardrail.stores.sqlite import SqliteBackend


class SqliteProvenanceStore:
    def __init__(self, backend: SqliteBackend) -> None:
        self.backend = backend

    async def register(self, session_id: str, refs: list[tuple[str, str]]) -> None:
        issued_at = now_iso()
        await self.backend.executemany(
            "INSERT OR IGNORE INTO provenance (session_id, entity_type, entity_id, issued_at)"
            " VALUES (?, ?, ?, ?)",
            [(session_id, entity_type, entity_id, issued_at) for entity_type, entity_id in refs],
        )
        await self.backend.commit()

    async def contains(self, session_id: str, entity_type: str, entity_id: str) -> bool:
        row = await self.backend.fetchone(
            "SELECT 1 FROM provenance"
            " WHERE session_id = ? AND entity_type = ? AND entity_id = ?",
            (session_id, entity_type, entity_id),
        )
        return row is not None
```

- [x] **Step 5: 在 `SqliteBackend` 上加 `executemany`**

`src/guardrail/stores/sqlite.py` 的 `SqliteBackend` 里，`execute` 之后追加：

```python
    async def executemany(
        self, sql: str, params: list[tuple[Any, ...]]
    ) -> aiosqlite.Cursor:
        return await self.conn.executemany(sql, params)
```

- [x] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_provenance.py -v`
Expected: 17 passed

- [x] **Step 7: Commit**

```bash
git add src/guardrail/provenance.py src/guardrail/stores/provenance.py \
  src/guardrail/stores/sqlite.py tests/test_provenance.py
git commit -m "feat: Provenance 门——声明式产出路径与写操作前置校验"
```

---

### Task 6: 单次判定链

**Files:**
- Create: `src/guardrail/policy/engine.py`
- Test: `tests/test_single_policy.py`

**Interfaces:**
- Consumes: `SessionRecord`（Task 1）、`ProvenanceStore` / `check_requirements`（Task 5）、`SingleCallPolicy` / `ExpressionError`（Task 2、4）、`validate_args`（Task 3）、`TOOL_SPECS`（Task 3）
- Produces:
  - `guardrail.policy.engine.SingleVerdict(decision: Literal["allow","deny"], kind: Literal["policy","invalid_args"], rule_id: str | None, reasons: list[str])`
  - `guardrail.policy.engine.is_session_valid(record, now_iso) -> bool`
  - `guardrail.policy.engine.evaluate_single_call(*, record, tool, args, policy, provenance_store, shop) -> SingleVerdict`
  - `guardrail.policy.engine.STEP_ORDER: tuple[str, ...]`（记录判定链顺序，供文档与测试引用）

- [x] **Step 1: 写失败测试 `tests/test_single_policy.py`**

```python
import httpx
import pytest

from guardrail.clock import now_iso
from guardrail.policy.engine import STEP_ORDER, evaluate_single_call, is_session_valid
from guardrail.policy.loader import load_policy
from guardrail.policy.single import SingleCallPolicy
from guardrail.protocols import SessionRecord
from guardrail.provenance import ProvenanceStore

POLICY = load_policy("policies/single_call.yaml")

# 判定链的前 6 步全是语法层，不碰商城。Task 9 会把第 7 步（结果态上限）接上，
# 而仓库策略里 create_order 带一条 cap 规则——那会让本文件的 create_order 用例
# 在 Task 9 之后突然需要真实商城。所以这里显式用一份「剥掉结果层规则」的策略，
# 把本文件的作用域钉死在语法层：第 7 步由 tests/test_caps.py 负责。
SYNTAX_ONLY_POLICY = SingleCallPolicy(
    version=POLICY.version,
    permissions=POLICY.permissions,
    rules=[r for r in POLICY.rules if r.cap_field is None],
)


def _unreachable_shop(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"语法层判定不该发起商城调用，却请求了 {request.url}")


class FakeProvenance:
    """只实现判定链用到的两个方法。真实实现有 SQLite 往返，单测里不需要。"""

    def __init__(self, known: set[tuple[str, str, str]] | None = None) -> None:
        self.known = known or set()

    async def register(self, session_id, refs) -> None:
        for entity_type, entity_id in refs:
            self.known.add((session_id, entity_type, entity_id))

    async def contains(self, session_id, entity_type, entity_id) -> bool:
        return (session_id, entity_type, entity_id) in self.known


def _record(agent_id: str = "ops_agent", expires_at: str | None = None) -> SessionRecord:
    return SessionRecord(
        session_id="s-1",
        agent_id=agent_id,
        task_id=None,
        created_at=now_iso(),
        expires_at=expires_at or "2999-01-01T00:00:00+00:00",
    )


async def _evaluate(record, tool, args, prov=None, policy=SYNTAX_ONLY_POLICY):
    # shop 用一个「任何请求都报错」的 transport：如果某条本该纯语法的用例
    # 意外走到第 7 步，这里会立刻炸，而不是静默发一个注定失败的请求。
    async with httpx.AsyncClient(transport=httpx.MockTransport(_unreachable_shop)) as shop:
        return await evaluate_single_call(
            record=record,
            tool=tool,
            args=args,
            policy=policy,
            provenance_store=prov or FakeProvenance(),
            shop=shop,
        )


# ---------- 判定链顺序本身 ----------

def test_step_order_is_the_documented_one():
    assert STEP_ORDER == (
        "session",
        "tool_known",
        "provenance",
        "args_schema",
        "permission",
        "single_threshold",
        "resulting_state_cap",
    )


# ---------- 第 1 步：会话 ----------

def test_session_valid_when_not_expired():
    assert is_session_valid(_record(), now_iso())


def test_session_invalid_when_expired():
    assert not is_session_valid(_record(expires_at="2000-01-01T00:00:00+00:00"), now_iso())


def test_session_invalid_when_expiry_unparsable():
    # 解析不了就当过期：宁可多拒一次，也不能让一个坏时间戳换来一个永久会话。
    assert not is_session_valid(_record(expires_at="not-a-timestamp"), now_iso())


async def test_expired_session_denied():
    verdict = await _evaluate(
        _record(expires_at="2000-01-01T00:00:00+00:00"), "list_products", {}
    )
    assert verdict.decision == "deny"
    assert "过期" in verdict.reasons[0]


# ---------- 第 2 步：工具白名单 ----------

async def test_unknown_tool_denied():
    verdict = await _evaluate(_record(), "teleport", {})
    assert verdict.decision == "deny"
    assert "teleport" in verdict.reasons[0]


# ---------- 第 3 步：provenance ----------

async def test_write_on_unseen_entity_denied():
    verdict = await _evaluate(_record(), "update_price", {"product_id": "p-x", "delta_pct": -1})
    assert verdict.decision == "deny"
    assert "Provenance" in verdict.reasons[0]


async def test_write_on_seen_entity_passes_provenance():
    prov = FakeProvenance({("s-1", "product", "p-x")})
    verdict = await _evaluate(
        _record(), "update_price", {"product_id": "p-x", "delta_pct": -1.0}, prov
    )
    assert verdict.decision == "allow"


# ---------- 第 4 步：参数契约 ----------

async def test_invalid_args_is_a_distinct_kind():
    verdict = await _evaluate(_record(), "get_product", {})
    assert verdict.decision == "deny"
    # 畸形参数是调用方的错（400），不是策略违规（403）——Agent 靠这个区分
    # 「我参数写错了」和「我不被允许」。
    assert verdict.kind == "invalid_args"


async def test_invalid_args_message_names_tool_and_field():
    verdict = await _evaluate(_record(), "get_product", {})
    text = " ".join(verdict.reasons)
    assert "get_product" in text
    assert "product_id" in text


# ---------- 第 5 步：权限 ----------

async def test_agent_without_permission_denied():
    verdict = await _evaluate(_record(agent_id="risk_auditor"), "update_price",
                              {"product_id": "p", "delta_pct": -1.0})
    assert verdict.decision == "deny"
    assert "无权" in verdict.reasons[0]


async def test_unlisted_agent_denied_everything():
    verdict = await _evaluate(_record(agent_id="ghost"), "list_products", {})
    assert verdict.decision == "deny"


async def test_pricing_agent_cannot_create_coupon():
    verdict = await _evaluate(
        _record(agent_id="pricing_agent"),
        "create_coupon",
        {"code": "X", "discount_pct": 20.0, "max_uses": 5},
    )
    assert verdict.decision == "deny"


async def test_permission_is_checked_after_provenance():
    # 顺序有意义：先看「这个工具动不动得了别的实体」，再看「你能不能动」。
    # 两条都失败时，provenance 的原因先出现——它更接近攻击形态。
    verdict = await _evaluate(_record(agent_id="risk_auditor"), "update_price",
                              {"product_id": "p-x", "delta_pct": -1.0})
    assert "Provenance" in verdict.reasons[0]


# ---------- 第 6 步：单次阈值 ----------

async def test_single_price_cut_over_10_denied():
    prov = FakeProvenance({("s-1", "product", "p-x")})
    verdict = await _evaluate(
        _record(), "update_price", {"product_id": "p-x", "delta_pct": -50.0}, prov
    )
    assert verdict.decision == "deny"
    assert verdict.rule_id == "max_single_price_cut"
    assert "10%" in verdict.reasons[0]


async def test_single_price_cut_exactly_10_allowed():
    prov = FakeProvenance({("s-1", "product", "p-x")})
    verdict = await _evaluate(
        _record(), "update_price", {"product_id": "p-x", "delta_pct": -10.0}, prov
    )
    assert verdict.decision == "allow"


async def test_price_increase_is_also_bounded():
    prov = FakeProvenance({("s-1", "product", "p-x")})
    verdict = await _evaluate(
        _record(), "update_price", {"product_id": "p-x", "delta_pct": 50.0}, prov
    )
    assert verdict.decision == "deny"


async def test_stock_delta_bounded():
    prov = FakeProvenance({("s-1", "product", "p-x")})
    ok = await _evaluate(
        _record(), "update_stock", {"product_id": "p-x", "delta": -500}, prov
    )
    too_far = await _evaluate(
        _record(), "update_stock", {"product_id": "p-x", "delta": -501}, prov
    )
    assert ok.decision == "allow"
    assert too_far.decision == "deny"
    assert too_far.rule_id == "max_single_stock_delta"


async def test_coupon_discount_threshold_leaves_room_for_combined_risk():
    args = {"code": "X", "discount_pct": 60.0, "max_uses": 5}
    assert (await _evaluate(_record(), "create_coupon", args)).decision == "allow"
    args = {"code": "X", "discount_pct": 90.0, "max_uses": 5}
    verdict = await _evaluate(_record(), "create_coupon", args)
    assert verdict.decision == "deny"
    assert verdict.rule_id == "max_coupon_discount"


async def test_discounted_bulk_order_denied():
    # 刻意用小额商品 p-tshirt-s：这条只验语法层第 6 步，不希望第 7 步的
    # 金额上限（50000 分）掺进来。p-iphone 一件就 599900 分，会被第 7 步拦掉，
    # 那样测的就不是本条规则了。
    prov = FakeProvenance({("s-1", "product", "p-tshirt-s"), ("s-1", "coupon", "c-1")})
    ok = await _evaluate(
        _record(), "create_order", {"product_id": "p-tshirt-s", "qty": 49, "coupon_id": "c-1"}, prov
    )
    too_many = await _evaluate(
        _record(), "create_order", {"product_id": "p-tshirt-s", "qty": 50, "coupon_id": "c-1"}, prov
    )
    assert ok.decision == "allow"
    assert too_many.decision == "deny"
    assert too_many.rule_id == "no_discounted_bulk_order"


async def test_bulk_order_without_coupon_is_allowed_by_that_rule():
    prov = FakeProvenance({("s-1", "product", "p-tshirt-s")})
    verdict = await _evaluate(
        _record(), "create_order", {"product_id": "p-tshirt-s", "qty": 500}, prov
    )
    assert verdict.decision == "allow"


# ---------- fail-closed ----------

async def test_expression_failure_denies(monkeypatch):
    import guardrail.policy.engine as engine

    def boom(_expression, _args):
        from guardrail.policy.expr import ExpressionError

        raise ExpressionError("模拟求值失败")

    monkeypatch.setattr(engine, "evaluate", boom)
    prov = FakeProvenance({("s-1", "product", "p-x")})
    verdict = await _evaluate(
        _record(), "update_price", {"product_id": "p-x", "delta_pct": -50.0}, prov
    )
    assert verdict.decision == "deny"
    assert "求值失败" in " ".join(verdict.reasons)


async def test_first_matching_rule_wins():
    prov = FakeProvenance({("s-1", "product", "p-x")})
    verdict = await _evaluate(
        _record(), "update_price", {"product_id": "p-x", "delta_pct": -99.0}, prov
    )
    assert verdict.rule_id == "max_single_price_cut"
    assert len(verdict.reasons) == 1
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_single_policy.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'guardrail.policy.engine'`

- [x] **Step 3: 写 `src/guardrail/policy/engine.py`**

```python
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from guardrail.policy.expr import ExpressionError, evaluate
from guardrail.policy.single import SingleCallPolicy
from guardrail.provenance import ProvenanceStore, check_requirements
from guardrail.protocols import SessionRecord
from guardrail.tools.contracts import ArgsValidationError, validate_args
from guardrail.tools.registry import TOOL_SPECS

# 判定链顺序（spec §6.1）。写成模块级常量是为了让顺序本身可被测试与文档引用——
# 「顺序不可调换」这句话如果没有一个可断言的对象，就只是一句注释。
#
# 与 spec §6.1 编号的两处有意偏离：
#   1. provenance（第 2 步）与工具白名单（第 3 步）互换。provenance 要读工具
#      自己声明的 requires，白名单都还没过就没有 requires 可读。
#   2. 幂等（spec §6.1 第 8 步）不在这里——它必须横跨「审计 → 执行 → 记账」，
#      塞不进一条纯前置的判定链。它由 api/tools.py 在执行前后两步完成，
#      理由见那里的说明。
STEP_ORDER: tuple[str, ...] = (
    "session",
    "tool_known",
    "provenance",
    "args_schema",
    "permission",
    "single_threshold",
    "resulting_state_cap",
)


class SingleVerdict(BaseModel):
    """单次策略的判定结果。

    `kind` 区分「参数畸形」与「策略违规」：前者是调用方的错（HTTP 400），
    后者是请求本身不被允许（HTTP 403）。Agent 靠这个区分该改参数还是该换做法。
    结果层规则由 policy/caps.py 在本模块之后接上，两者是同一个 verdict 对象。
    """

    decision: Literal["allow", "deny"]
    kind: Literal["policy", "invalid_args"] = "policy"
    rule_id: str | None = None
    reasons: list[str] = Field(default_factory=list)


def _deny(reason: str, *, kind: str = "policy", rule_id: str | None = None) -> SingleVerdict:
    return SingleVerdict(decision="deny", kind=kind, rule_id=rule_id, reasons=[reason])


def is_session_valid(record: SessionRecord, now: str) -> bool:
    """会话是否存在且未过期。

    时间戳解析失败按过期处理：多拒一次的代价，远小于让一个坏时间戳换来一个
    永久有效的会话。
    """
    try:
        expires_at = datetime.fromisoformat(record.expires_at)
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        return False
    return expires_at > datetime.fromisoformat(now)


async def evaluate_single_call(
    *,
    record: SessionRecord,
    tool: str,
    args: dict[str, Any],
    policy: SingleCallPolicy,
    provenance_store: ProvenanceStore,
    shop: httpx.AsyncClient,
) -> SingleVerdict:
    """按 STEP_ORDER 求值一次工具调用。

    `shop` 只被结果层上限（policy/caps.py，Task 9 接上）使用；本任务里所有已
    配置的语法层规则都不需要它。
    """
    from guardrail.clock import now_iso

    # 1 会话有效性
    if not is_session_valid(record, now_iso()):
        return _deny(f"会话已过期或不可用：{record.session_id}")

    # 2 工具白名单
    spec = TOOL_SPECS.get(tool)
    if spec is None:
        return _deny(f"未知工具：{tool}")

    # 3 Provenance
    reason = await check_requirements(provenance_store, record.session_id, spec, args)
    if reason is not None:
        return _deny(reason)

    # 4 参数契约
    try:
        validate_args(spec, args)
    except ArgsValidationError as exc:
        return _deny(str(exc), kind="invalid_args")

    # 5 权限
    if not policy.is_permitted(record.agent_id, tool):
        return _deny(f"agent {record.agent_id!r} 无权调用工具 {tool!r}")

    # 6 单次阈值（语法层）
    for rule in policy.rules_for(tool):
        if rule.deny_if is None:
            continue
        try:
            hit = bool(evaluate(rule.deny_if, args))
        except ExpressionError as exc:
            # 表达式求值失败按拒绝处理，而不是按「没命中」放行（spec §10.2）。
            return _deny(
                f"规则 {rule.id!r} 求值失败，已按 fail-closed 拒绝本次调用：{exc}",
                rule_id=rule.id,
            )
        if hit:
            return _deny(rule.message, rule_id=rule.id)

    # 7 结果态上限由 policy/caps.py 追加（Task 9）
    return SingleVerdict(decision="allow")
```

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_single_policy.py -v`
Expected: 27 passed

- [x] **Step 5: Commit**

```bash
git add src/guardrail/policy/engine.py tests/test_single_policy.py
git commit -m "feat: 单次判定链——会话/白名单/Provenance/参数契约/权限/单次阈值，fail-closed"
```

---

### Task 7: 审计链

**Files:**
- Create: `src/guardrail/audit.py`
- Create: `src/guardrail/stores/audit.py`
- Create: `src/guardrail/api/audit.py`
- Test: `tests/test_audit_chain.py`

**Interfaces:**
- Consumes: `SqliteBackend`（Task 1）、`now_iso`（Task 1）
- Produces:
  - `guardrail.audit.GENESIS_HASH: str`
  - `guardrail.audit.canonical_json(value) -> str`
  - `guardrail.audit.sha256_hex(text) -> str`
  - `guardrail.audit.AuditDraft`（`session_id`、`plan_id`、`tool`、`args`、`decision`、`replay`、`reasons`、`timestamp`）
  - `guardrail.audit.AuditEntry(AuditDraft)`（追加 `seq`、`prev_hash`、`entry_hash`），方法 `hashed_payload() -> dict`
  - `guardrail.audit.ChainVerdict(ok, checked, broken_at_seq, reason)`
  - `guardrail.audit.AuditSink`（Protocol）：`async append(draft) -> AuditEntry`、`async verify_chain(since_seq=0) -> ChainVerdict`、`async head_hash() -> str`、`async list_entries(session_id, limit) -> list[AuditEntry]`
  - `guardrail.stores.audit.SqliteAuditSink(backend)`
  - `GET /v1/audit/verify` → `ChainVerdict`

- [x] **Step 1: 写失败测试 `tests/test_audit_chain.py`**

```python
import pytest

from guardrail.audit import (
    GENESIS_HASH,
    AuditDraft,
    canonical_json,
    compute_entry_hash,
    sha256_hex,
)
from guardrail.stores.audit import SqliteAuditSink
from guardrail.stores.sqlite import SqliteBackend


@pytest.fixture
async def sink(tmp_path):
    backend = SqliteBackend(str(tmp_path / "gateway.db"))
    await backend.connect()
    yield SqliteAuditSink(backend)
    await backend.close()


def _draft(tool: str = "update_price", **kwargs) -> AuditDraft:
    base = {
        "session_id": "s-1",
        "plan_id": None,
        "tool": tool,
        "args": {"product_id": "p-1", "delta_pct": -5.0},
        "decision": "allow",
        "replay": False,
        "reasons": [],
        "timestamp": "2026-10-04T10:00:00+00:00",
    }
    return AuditDraft(**{**base, **kwargs})


# ---------- 规范化与哈希 ----------

def test_canonical_json_is_key_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_keeps_chinese_readable():
    assert "单次降价" in canonical_json({"message": "单次降价不得超过 10%"})


def test_canonical_json_has_no_padding():
    assert canonical_json({"a": 1, "b": 2}) == '{"a":1,"b":2}'


def test_entry_hash_depends_on_prev_hash():
    a = compute_entry_hash(GENESIS_HASH, {"x": 1})
    b = compute_entry_hash("f" * 64, {"x": 1})
    assert a != b


def test_entry_hash_is_sha256_of_prev_plus_payload():
    assert compute_entry_hash("a" * 64, {"x": 1}) == sha256_hex('a' * 64 + '{"x":1}')


def test_genesis_hash_is_64_zeros():
    assert GENESIS_HASH == "0" * 64


# ---------- 追加 ----------

async def test_first_entry_chains_from_genesis(sink):
    entry = await sink.append(_draft())
    assert entry.seq == 1
    assert entry.prev_hash == GENESIS_HASH
    assert len(entry.entry_hash) == 64


async def test_entries_chain_to_each_other(sink):
    first = await sink.append(_draft())
    second = await sink.append(_draft(tool="update_stock"))
    assert second.prev_hash == first.entry_hash


async def test_hash_is_recomputable_from_stored_fields(sink):
    entry = await sink.append(_draft())
    assert (
        compute_entry_hash(entry.prev_hash, entry.hashed_payload()) == entry.entry_hash
    )


async def test_hashed_payload_excludes_hash_fields():
    entry = await sink.append(_draft())
    payload = entry.hashed_payload()
    assert "entry_hash" not in payload
    assert "prev_hash" not in payload
    assert "seq" not in payload


async def test_head_hash_reflects_latest(sink):
    assert await sink.head_hash() == GENESIS_HASH
    entry = await sink.append(_draft())
    assert await sink.head_hash() == entry.entry_hash


async def test_list_entries_filters_by_session(sink):
    await sink.append(_draft(session_id="s-1"))
    await sink.append(_draft(session_id="s-2"))
    entries = await sink.list_entries("s-1")
    assert [e.session_id for e in entries] == ["s-1"]


async def test_list_entries_respects_limit(sink):
    for _ in range(5):
        await sink.append(_draft())
    assert len(await sink.list_entries("s-1", limit=2)) == 2


async def test_deny_entries_are_recorded(sink):
    entry = await sink.append(_draft(decision="deny", reasons=["单次降价不得超过 10%"]))
    assert entry.decision == "deny"
    assert entry.reasons == ["单次降价不得超过 10%"]


async def test_replay_entries_are_recorded(sink):
    entry = await sink.append(_draft(replay=True))
    assert entry.replay is True


# ---------- 校验 ----------

async def test_empty_chain_verifies(sink):
    verdict = await sink.verify_chain()
    assert verdict.ok is True
    assert verdict.checked == 0


async def test_valid_chain_verifies(sink):
    for _ in range(3):
        await sink.append(_draft())
    verdict = await sink.verify_chain()
    assert verdict.ok is True
    assert verdict.checked == 3
    assert verdict.broken_at_seq is None


async def test_tampered_payload_breaks_chain(sink):
    await sink.append(_draft())
    await sink.append(_draft(tool="update_stock"))
    await sink.execute_tamper_for_test(seq=1, args_json='{"product_id":"p-evil"}')
    verdict = await sink.verify_chain()
    assert verdict.ok is False
    assert verdict.broken_at_seq == 1
    assert "1" in (verdict.reason or "")


async def test_tampered_decision_breaks_chain(sink):
    await sink.append(_draft(decision="deny"))
    await sink.execute_tamper_for_test(seq=1, decision="allow")
    verdict = await sink.verify_chain()
    assert verdict.ok is False
    assert verdict.broken_at_seq == 1


async def test_deleted_entry_breaks_chain(sink):
    await sink.append(_draft())
    await sink.append(_draft())
    await sink.execute_tamper_for_test(seq=2, delete=True)
    verdict = await sink.verify_chain()
    assert verdict.ok is False


async def test_appended_forged_entry_breaks_chain(sink):
    # 攻击者能写库，但算不出正确的 entry_hash。
    await sink.append(_draft())
    await sink.execute_tamper_for_test(seq=2, forged=True)
    verdict = await sink.verify_chain()
    assert verdict.ok is False


async def test_verify_chain_since_seq_skips_earlier_entries(sink):
    for _ in range(3):
        await sink.append(_draft())
    verdict = await sink.verify_chain(since_seq=3)
    assert verdict.ok is True
    assert verdict.checked == 1


async def test_verify_chain_since_seq_beyond_head_is_ok(sink):
    await sink.append(_draft())
    verdict = await sink.verify_chain(since_seq=99)
    assert verdict.ok is True
    assert verdict.checked == 0
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_audit_chain.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'guardrail.audit'`

- [x] **Step 3: 写 `src/guardrail/audit.py`**

```python
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

# 链的首条记录的 prev_hash。它让「第一条」和「第 N 条」走同一条代码路径。
GENESIS_HASH = "0" * 64

Decision = Literal["allow", "allow_with_flag", "ask", "deny"]


def canonical_json(value: Any) -> str:
    """稳定的 JSON 序列化。

    sort_keys 保证键序无关；separators 去掉空白；ensure_ascii=False 让中文留在
    链上可读——转成 \\uXXXX 同样是确定性的，但审计链的主要读者是人。

    这个函数被幂等键和审计链共用，所以它一旦改动，两边的历史数据会同时失效。
    真要改，必须配一次数据迁移；不要为了「统一风格」动它。
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_entry_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    return sha256_hex(prev_hash + canonical_json(payload))


class AuditDraft(BaseModel):
    """一条待追加的审计记录（还没有链的位置信息）。"""

    session_id: str
    plan_id: str | None = None
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    decision: Decision
    # replay 单独成字段而不是塞进 decision：decision 是策略判定，会被
    # /metrics 按值计数（spec §19.4），掺进一个非判定值会污染那个指标。
    # spec §6.2 原文写的是 `decision: replay`，此处有意偏离，理由同上。
    replay: bool = False
    reasons: list[str] = Field(default_factory=list)
    timestamp: str


class AuditEntry(AuditDraft):
    """链上的一条记录。"""

    seq: int
    prev_hash: str
    entry_hash: str

    def hashed_payload(self) -> dict[str, Any]:
        """参与哈希的字段。

        刻意不含 seq / prev_hash / entry_hash 自身：prev_hash 已经以
        「前缀拼接」的方式参与了计算（spec §7），再放进 payload 会重复计入。
        """
        return {
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "tool": self.tool,
            "args": self.args,
            "decision": self.decision,
            "replay": self.replay,
            "reasons": self.reasons,
            "timestamp": self.timestamp,
        }


class ChainVerdict(BaseModel):
    ok: bool
    checked: int
    broken_at_seq: int | None = None
    reason: str | None = None


class AuditSink(Protocol):
    """审计后端（spec §19.1 的第二个接缝）。

    审计是唯一必须比业务更持久的东西，所以它是独立接缝而不是业务表的一个
    视图。当前实现是 SQLite 哈希链；预留实现是 WORM 存储 / 外部日志。
    """

    async def append(self, draft: AuditDraft) -> AuditEntry: ...

    async def verify_chain(self, since_seq: int = 0) -> ChainVerdict: ...

    async def head_hash(self) -> str: ...

    async def list_entries(self, session_id: str, limit: int = 200) -> list[AuditEntry]: ...
```

- [x] **Step 4: 写 `src/guardrail/stores/audit.py`**

```python
from __future__ import annotations

import asyncio
import json

from guardrail.audit import (
    GENESIS_HASH,
    AuditDraft,
    AuditEntry,
    ChainVerdict,
    compute_entry_hash,
    sha256_hex,
)
from guardrail.stores.sqlite import SqliteBackend


class SqliteAuditSink:
    """SQLite 哈希链。

    append 在进程内用一把 asyncio 锁串行化。理由不是「SQLite 慢」，而是
    「读 prev_hash → 算 hash → 插入」这三步必须不可交错：两个并发的 append
    会读到同一个 prev_hash，链条从此断裂。锁的范围只覆盖这三步。
    """

    def __init__(self, backend: SqliteBackend) -> None:
        self.backend = backend
        self._lock = asyncio.Lock()

    async def append(self, draft: AuditDraft) -> AuditEntry:
        async with self._lock:
            prev_hash = await self._head_hash_unlocked()
            payload = draft.model_dump()
            entry_hash = compute_entry_hash(prev_hash, payload)
            cur = await self.backend.execute(
                "INSERT INTO audit_log"
                " (prev_hash, entry_hash, session_id, plan_id, tool, args_json,"
                "  decision, replay, reasons_json, timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    prev_hash,
                    entry_hash,
                    draft.session_id,
                    draft.plan_id,
                    draft.tool,
                    json.dumps(draft.args, ensure_ascii=False),
                    draft.decision,
                    1 if draft.replay else 0,
                    json.dumps(draft.reasons, ensure_ascii=False),
                    draft.timestamp,
                ),
            )
            await self.backend.commit()
            seq = cur.lastrowid
        if seq is None:  # pragma: no cover - aiosqlite 一定会填 lastrowid
            raise RuntimeError("审计链插入后拿不到 seq")
        return AuditEntry(seq=seq, prev_hash=prev_hash, entry_hash=entry_hash, **payload)

    async def _head_hash_unlocked(self) -> str:
        row = await self.backend.fetchone("SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1")
        return row["entry_hash"] if row is not None else GENESIS_HASH

    async def head_hash(self) -> str:
        return await self._head_hash_unlocked()

    async def list_entries(self, session_id: str, limit: int = 200) -> list[AuditEntry]:
        rows = await self.backend.fetchall(
            "SELECT * FROM audit_log WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
            (session_id, limit),
        )
        return [_to_entry(r) for r in rows]

    async def verify_chain(self, since_seq: int = 0) -> ChainVerdict:
        rows = await self.backend.fetchall(
            "SELECT * FROM audit_log WHERE seq >= ? ORDER BY seq", (since_seq,)
        )
        if not rows:
            return ChainVerdict(ok=True, checked=0)

        if since_seq > 0:
            prior = await self.backend.fetchone(
                "SELECT entry_hash FROM audit_log WHERE seq = ?", (since_seq - 1,)
            )
            expected_prev = prior["entry_hash"] if prior is not None else GENESIS_HASH
        else:
            expected_prev = GENESIS_HASH

        for row in rows:
            entry = _to_entry(row)
            if entry.prev_hash != expected_prev:
                return ChainVerdict(
                    ok=False,
                    checked=len(rows),
                    broken_at_seq=entry.seq,
                    reason=(
                        f"seq={entry.seq} 的 prev_hash 与前一条不符："
                        f"记录 {entry.prev_hash[:8]}，实际应为 {expected_prev[:8]}"
                    ),
                )
            recomputed = compute_entry_hash(entry.prev_hash, entry.hashed_payload())
            if recomputed != entry.entry_hash:
                return ChainVerdict(
                    ok=False,
                    checked=len(rows),
                    broken_at_seq=entry.seq,
                    reason=(
                        f"seq={entry.seq} 的 payload 已被篡改："
                        f"重算得 {recomputed[:8]}，记录为 {entry.entry_hash[:8]}"
                    ),
                )
            expected_prev = entry.entry_hash

        return ChainVerdict(ok=True, checked=len(rows))

    async def execute_tamper_for_test(
        self,
        seq: int,
        *,
        args_json: str | None = None,
        decision: str | None = None,
        delete: bool = False,
        forged: bool = False,
    ) -> None:
        """直接改写底层存储——**只给测试用**。

        审计链的验收标准是「篡改必须被发现」（spec §7），而篡改按定义不能
        经过 AuditSink 的公开接口。这个方法就是那条「后门」，因此它的存在本身
        就是「哈希链只防外部篡改、不防有写库权限的人」这一事实的证据——
        README 与 docs/limitations.md 要写明这一点。
        """
        if delete:
            await self.backend.execute("DELETE FROM audit_log WHERE seq = ?", (seq,))
            await self.backend.commit()
            return
        if args_json is not None:
            await self.backend.execute(
                "UPDATE audit_log SET args_json = ? WHERE seq = ?", (args_json, seq)
            )
        if decision is not None:
            await self.backend.execute(
                "UPDATE audit_log SET decision = ? WHERE seq = ?", (decision, seq)
            )
        if forged:
            await self.backend.execute(
                "UPDATE audit_log SET entry_hash = ? WHERE seq = ?",
                (sha256_hex("forged"), seq),
            )
        await self.backend.commit()


def _to_entry(row: object) -> AuditEntry:
    return AuditEntry(
        seq=row["seq"],  # type: ignore[index]
        prev_hash=row["prev_hash"],  # type: ignore[index]
        entry_hash=row["entry_hash"],  # type: ignore[index]
        session_id=row["session_id"],  # type: ignore[index]
        plan_id=row["plan_id"],  # type: ignore[index]
        tool=row["tool"],  # type: ignore[index]
        args=json.loads(row["args_json"]),  # type: ignore[index]
        decision=row["decision"],  # type: ignore[index]
        replay=bool(row["replay"]),  # type: ignore[index]
        reasons=json.loads(row["reasons_json"]),  # type: ignore[index]
        timestamp=row["timestamp"],  # type: ignore[index]
    )
```

- [x] **Step 5: 写 `src/guardrail/api/audit.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Request

from guardrail.audit import ChainVerdict

router = APIRouter(prefix="/v1", tags=["audit"])


@router.get("/audit/verify", response_model=ChainVerdict)
async def verify_audit(request: Request) -> ChainVerdict:
    """校验整条审计链。

    这是「审计链可验证」这件事最早的对外证据：控制台（M5）之前，先有一个
    能跑的 HTTP 入口。控制台做出来之后它会退居幕后，但不会消失——
    CI 的 `make verify` 会调它。
    """
    return await request.app.state.audit.verify_chain()
```

- [x] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_audit_chain.py -v`
Expected: 27 passed

- [x] **Step 7: Commit**

```bash
git add src/guardrail/audit.py src/guardrail/stores/audit.py src/guardrail/api/audit.py \
  tests/test_audit_chain.py
git commit -m "feat: 审计哈希链——追加、链校验与篡改检测"
```

---

### Task 8: 幂等

**Files:**
- Create: `src/guardrail/idempotency.py`
- Create: `src/guardrail/stores/idempotency.py`
- Test: `tests/test_idempotency.py`

**Interfaces:**
- Consumes: `canonical_json` / `sha256_hex`（Task 7）、`SqliteBackend`（Task 1）
- Produces:
  - `guardrail.idempotency.IdempotencyRecord(key, session_id, status, response, created_at)`
  - `guardrail.idempotency.IdempotencyStore`（Protocol）：`async get(key)`、`async begin(key, session_id) -> bool`、`async complete(key, response) -> None`、`async release(key) -> None`
  - `guardrail.idempotency.idempotency_key(session_id, tool, args) -> str`
  - `guardrail.stores.idempotency.SqliteIdempotencyStore(backend)`

- [x] **Step 1: 写失败测试 `tests/test_idempotency.py`**

```python
import pytest

from guardrail.idempotency import idempotency_key
from guardrail.stores.idempotency import SqliteIdempotencyStore
from guardrail.stores.sqlite import SqliteBackend

ARGS = {"product_id": "p-1", "delta_pct": -5.0}


@pytest.fixture
async def store(tmp_path):
    backend = SqliteBackend(str(tmp_path / "gateway.db"))
    await backend.connect()
    yield SqliteIdempotencyStore(backend)
    await backend.close()


# ---------- 键 ----------

def test_key_is_stable_across_calls():
    assert idempotency_key("s-1", "update_price", ARGS) == idempotency_key(
        "s-1", "update_price", dict(ARGS)
    )


def test_key_ignores_arg_order():
    a = idempotency_key("s-1", "t", {"x": 1, "y": 2})
    b = idempotency_key("s-1", "t", {"y": 2, "x": 1})
    assert a == b


def test_key_varies_by_session():
    assert idempotency_key("s-1", "t", ARGS) != idempotency_key("s-2", "t", ARGS)


def test_key_varies_by_tool():
    assert idempotency_key("s-1", "update_price", ARGS) != idempotency_key(
        "s-1", "update_stock", ARGS
    )


def test_key_varies_by_args():
    assert idempotency_key("s-1", "t", {"a": 1}) != idempotency_key("s-1", "t", {"a": 2})


def test_key_is_hex_sha256():
    key = idempotency_key("s-1", "t", ARGS)
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_key_has_no_field_delimiter_ambiguity():
    # 拼接式哈希（session ‖ tool ‖ json）会碰撞：("ab","c",{}) 与 ("a","bc",{})
    # 拼出同一个字符串。整体 canonical_json 之后哈希就没有这个问题。
    assert idempotency_key("ab", "c", {}) != idempotency_key("a", "bc", {})


# ---------- 三种命中情况 ----------

async def test_miss_then_begin_succeeds(store):
    assert await store.get(idempotency_key("s-1", "t", ARGS)) is None
    assert await store.begin(idempotency_key("s-1", "t", ARGS), "s-1") is True


async def test_in_progress_blocks_second_begin(store):
    key = idempotency_key("s-1", "t", ARGS)
    assert await store.begin(key, "s-1") is True
    assert await store.begin(key, "s-1") is False
    record = await store.get(key)
    assert record is not None
    assert record.status == "in_progress"


async def test_done_stores_response(store):
    key = idempotency_key("s-1", "t", ARGS)
    await store.begin(key, "s-1")
    await store.complete(key, {"tool": "t", "result": {"ok": True}})
    record = await store.get(key)
    assert record is not None
    assert record.status == "done"
    assert record.response == {"tool": "t", "result": {"ok": True}}


async def test_complete_preserves_original_timestamp(store):
    key = idempotency_key("s-1", "t", ARGS)
    await store.begin(key, "s-1")
    first = await store.get(key)
    await store.complete(key, {"x": 1})
    second = await store.get(key)
    assert first is not None and second is not None
    assert first.created_at == second.created_at


async def test_release_allows_retry(store):
    # 执行失败时必须释放：否则一次 409 就会把这个键在会话 TTL 内永久堵死，
    # 而那次调用其实什么也没做成。
    key = idempotency_key("s-1", "t", ARGS)
    await store.begin(key, "s-1")
    await store.release(key)
    assert await store.get(key) is None
    assert await store.begin(key, "s-1") is True


async def test_release_on_missing_key_is_noop(store):
    await store.release("nonexistent")


async def test_begin_records_session_id(store):
    key = idempotency_key("s-1", "t", ARGS)
    await store.begin(key, "s-1")
    record = await store.get(key)
    assert record is not None
    assert record.session_id == "s-1"


async def test_complete_on_missing_key_raises(store):
    # 静默成功会让「忘记 begin」的 bug 消失在日志里。
    with pytest.raises(RuntimeError):
        await store.complete("nonexistent", {"x": 1})
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_idempotency.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'guardrail.idempotency'`

- [x] **Step 3: 写 `src/guardrail/idempotency.py`**

```python
from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from guardrail.audit import canonical_json, sha256_hex


def idempotency_key(session_id: str, tool: str, args: dict[str, Any]) -> str:
    """幂等键 = sha256(canonical_json({session_id, tool, args}))。

    spec §6.2 写的是 `sha256(session_id ‖ tool ‖ canonical_json(args))`。这里改成
    整体规范化之后哈希：字段直接拼接会碰撞——("ab", "c") 与 ("a", "bc") 拼出
    同一个字符串，于是两个不同的调用共用一个幂等键，后者的响应会被原样返回给
    前者。语义完全一致，消掉了分隔符歧义。

    `args` 必须是 **Agent 提交的原始参数**，不能是服务端派生之后的版本：
    派生结果依赖当时的商城状态，同一个意图在两次调用里会算出不同的
    absolute_delta_cents，键就永远对不上。
    """
    return sha256_hex(canonical_json({"session_id": session_id, "tool": tool, "args": args}))


class IdempotencyRecord(BaseModel):
    key: str
    session_id: str
    status: str  # in_progress | done
    response: dict[str, Any] | None = None
    created_at: str


class IdempotencyStore(Protocol):
    """幂等登记（spec §6.2）。

    幂等不是可选的健壮性装饰：Agent 会在超时、重试、网络抖动时重复发出同一个
    调用。没有幂等，一次「降价 5%」的重试就变成「降价 10%」——而这恰好绕过
    单次阈值，因为策略看到的是两个各自合规的 5%。幂等是单次阈值能成立的前提。
    """

    async def get(self, key: str) -> IdempotencyRecord | None: ...

    async def begin(self, key: str, session_id: str) -> bool:
        """登记一次进行中的调用。已被占用时返回 False（调用方应回 409）。"""
        ...

    async def complete(self, key: str, response: dict[str, Any]) -> None: ...

    async def release(self, key: str) -> None:
        """执行失败时释放登记，让 Agent 能真正重试。"""
        ...
```

- [x] **Step 4: 写 `src/guardrail/stores/idempotency.py`**

```python
from __future__ import annotations

import json

from guardrail.audit import canonical_json
from guardrail.clock import now_iso
from guardrail.idempotency import IdempotencyRecord
from guardrail.stores.sqlite import SqliteBackend


class SqliteIdempotencyStore:
    def __init__(self, backend: SqliteBackend) -> None:
        self.backend = backend

    async def get(self, key: str) -> IdempotencyRecord | None:
        row = await self.backend.fetchone("SELECT * FROM idempotency WHERE key = ?", (key,))
        if row is None:
            return None
        return IdempotencyRecord(
            key=row["key"],
            session_id=row["session_id"],
            status=row["status"],
            response=json.loads(row["response_json"]) if row["response_json"] else None,
            created_at=row["created_at"],
        )

    async def begin(self, key: str, session_id: str) -> bool:
        """登记一次进行中的调用。已被占用时返回 False。

        实现要点：先无条件占位插入（状态写 `done`、响应为空），再用一次带
        status 条件的 UPDATE 认领。两个细节都是必要的：

        - 「先查再插」在并发下有窗口。两个请求可能都查到「不存在」，然后都插入。
        - 占位时写 `done` 而不是 `in_progress`，认领时才改成 `in_progress`：这样
          一个**已完成**的键（`done` + 响应非空）永远不会被后来的 `begin` 抢走，
          而 `complete()` 推进到的 `done` 状态带非空响应，两种 `done` 不会混淆。
        """
        await self.backend.execute(
            "INSERT OR IGNORE INTO idempotency"
            " (key, session_id, status, response_json, created_at)"
            " VALUES (?, ?, 'done', NULL, ?)",
            (key, session_id, now_iso()),
        )
        cur = await self.backend.execute(
            "UPDATE idempotency SET status = 'in_progress', created_at = ?"
            " WHERE key = ? AND status = 'done' AND response_json IS NULL",
            (now_iso(), key),
        )
        await self.backend.commit()
        return (cur.rowcount or 0) > 0

    async def complete(self, key: str, response: dict[str, Any]) -> None:
        cur = await self.backend.execute(
            "UPDATE idempotency SET status = 'done', response_json = ? WHERE key = ?",
            (canonical_json(response), key),
        )
        await self.backend.commit()
        if cur.rowcount == 0:
            raise RuntimeError(f"幂等键未登记就 complete：{key}")

    async def release(self, key: str) -> None:
        await self.backend.execute("DELETE FROM idempotency WHERE key = ?", (key,))
        await self.backend.commit()
```

`test_in_progress_blocks_second_begin` 覆盖的正是「占位写 `done`、认领改 `in_progress`」这个设计：第一次 `begin` 认领成功，第二次因为状态已是 `in_progress` 而认领失败。

- [x] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_idempotency.py -v`
Expected: 15 passed

- [x] **Step 6: Commit**

```bash
git add src/guardrail/idempotency.py src/guardrail/stores/idempotency.py tests/test_idempotency.py
git commit -m "feat: 幂等键与登记存储——已完成/进行中/未命中三种状态"
```

---

### Task 9: 影子装载器与结果态上限

**Files:**
- Create: `src/guardrail/projection_args.py`（从 `api/tools.py` 迁出）
- Create: `src/guardrail/shadow_loader.py`
- Create: `src/guardrail/policy/caps.py`
- Modify: `src/guardrail/api/tools.py`（改为从新模块导入，删掉已迁出的代码）
- Modify: `src/guardrail/policy/engine.py`（接上第 7 步）
- Modify: `tests/test_projection_args.py`（改导入路径）
- Test: `tests/test_caps.py`

**Interfaces:**
- Consumes: `derive_projection_args`（计划 ①）、`ShadowState` / `project` / `build_shadow`、`assert_action_applicable`（计划 ①）、`TOOL_SPECS`（Task 3）
- Produces:
  - `guardrail.projection_args.PlannedAction` / `derive_projection_args`（原样迁移）
  - `guardrail.shadow_loader.ShadowLoadError`
  - `guardrail.shadow_loader.is_preview_id(value) -> bool`
  - `guardrail.shadow_loader.load_referenced_entities(shop, actions) -> tuple[list, list]`
  - `guardrail.shadow_loader.load_shadow(shop, calls) -> ShadowState`
  - `guardrail.shadow_loader.project_call(shop, tool, args) -> ShadowState`
  - `guardrail.policy.caps.CAP_ERROR_PREFIX`（供 API 归类）
  - `guardrail.policy.caps.evaluate_result_caps(*, tool, args, policy, shop) -> tuple[str, str] | None`（`(rule_id, 原因)` 或 `None`）

- [x] **Step 1: 写失败测试 `tests/test_caps.py`**

```python
import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.policy.caps import evaluate_result_caps
from guardrail.policy.loader import load_policy
from guardrail.shadow_loader import ShadowLoadError, is_preview_id, load_shadow, project_call
from shop.main import create_app as create_shop_app

POLICY = load_policy("policies/single_call.yaml")


@pytest.fixture
async def shop(tmp_path):
    app = create_shop_app(str(tmp_path / "shop.db"))
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://shop.test") as c:
            yield c


# ---------- 影子装载器 ----------

def test_is_preview_id():
    assert is_preview_id("preview-coupon-0")
    assert is_preview_id("preview-order-3")
    assert not is_preview_id("c-abc")
    assert not is_preview_id("o-abc")
    assert not is_preview_id(0)


async def test_load_shadow_includes_all_products(shop):
    shadow = await load_shadow(shop, [("get_product", {"product_id": "p-iphone"})])
    assert "p-iphone" in shadow.products
    assert shadow.products["p-iphone"].list_price_cents == 599900


async def test_load_shadow_raises_on_missing_coupon(shop):
    with pytest.raises(ShadowLoadError, match="c-nope"):
        await load_shadow(shop, [("create_order", {"product_id": "p", "qty": 1, "coupon_id": "c-nope"})])


async def test_load_shadow_skips_intra_plan_ids(shop):
    shadow = await load_shadow(
        shop,
        [
            ("create_coupon", {"code": "S20", "discount_pct": 20.0, "max_uses": 5}),
            ("create_order", {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": "preview-coupon-0"}),
        ],
    )
    assert "p-tshirt-s" in shadow.products


async def test_project_call_applies_effects(shop):
    shadow = await project_call(shop, "update_price", {"product_id": "p-iphone", "delta_pct": -10.0})
    assert shadow.products["p-iphone"].list_price_cents == round(599900 * 0.9)


async def test_project_call_rejects_impossible_price(shop):
    from guardrail.projection import ProjectionError

    with pytest.raises(ProjectionError):
        await project_call(shop, "update_price", {"product_id": "p-iphone", "delta_pct": -100.0})


# ---------- 结果层上限 ----------

async def test_small_order_passes_cap(shop):
    # p-tshirt-s 标价 9900 分，2 件 = 19800 分 < 50000
    assert await evaluate_result_caps(
        tool="create_order",
        args={"product_id": "p-tshirt-s", "qty": 2},
        policy=POLICY,
        shop=shop,
    ) is None


async def test_large_order_hits_cap(shop):
    # p-iphone 599900 分，1 件就超 50000
    hit = await evaluate_result_caps(
        tool="create_order",
        args={"product_id": "p-iphone", "qty": 1},
        policy=POLICY,
        shop=shop,
    )
    assert hit is not None
    rule_id, reason = hit
    assert rule_id == "cap_order_amount"
    assert "500.00" in reason


async def test_cap_boundary_is_inclusive(shop):
    # p-tshirt-s 标价 9900 分。上限 50000 分。
    # 5 件 = 49500 < 50000 放行；6 件 = 59400 > 50000 拒绝。
    assert await evaluate_result_caps(
        tool="create_order",
        args={"product_id": "p-tshirt-s", "qty": 5},
        policy=POLICY,
        shop=shop,
    ) is None
    hit = await evaluate_result_caps(
        tool="create_order",
        args={"product_id": "p-tshirt-s", "qty": 6},
        policy=POLICY,
        shop=shop,
    )
    assert hit is not None


async def test_cap_reads_projection_not_args(shop):
    # args 里谎报一个 total_amount 不应该影响判定——上限只看投影结果态。
    hit = await evaluate_result_caps(
        tool="create_order",
        args={"product_id": "p-iphone", "qty": 1, "total_amount": 1},
        policy=POLICY,
        shop=shop,
    )
    assert hit is not None
    assert hit[0] == "cap_order_amount"


async def test_cap_accounts_for_coupon_discount(shop):
    coupon = (await shop.post("/shop/v1/coupons", json={
        "code": "HALF", "discount_pct": 50.0, "max_uses": 5
    })).json()
    # p-tshirt-s 9900 → 五折 4950；2 件 = 9900 分，远低于上限。
    assert await evaluate_result_caps(
        tool="create_order",
        args={"product_id": "p-tshirt-s", "qty": 2, "coupon_id": coupon["id"]},
        policy=POLICY,
        shop=shop,
    ) is None


async def test_no_cap_rule_for_tool_means_no_check(shop):
    assert await evaluate_result_caps(
        tool="update_price", args={"product_id": "p-iphone", "delta_pct": -1.0},
        policy=POLICY, shop=shop,
    ) is None


async def test_projection_failure_fails_closed(shop):
    from guardrail.policy.caps import CapsEvaluationError

    with pytest.raises(CapsEvaluationError):
        await evaluate_result_caps(
            tool="create_order",
            args={"product_id": "p-tshirt-s", "qty": 1, "coupon_id": "c-does-not-exist"},
            policy=POLICY,
            shop=shop,
        )
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_caps.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'guardrail.shadow_loader'`

- [x] **Step 3: 建 `src/guardrail/projection_args.py`，把 `api/tools.py` 的相关代码迁过去**

`src/guardrail/projection_args.py` 的内容 = 计划 ① `api/tools.py` 里的这一段，**逐字迁移，不改逻辑**：
`PlannedAction` 类、`derive_projection_args`、`_require_float_arg`、`_require_int_arg`、`_require_optional_str_arg` 及其文档注释。文件头加 `from __future__ import annotations`，import 段为：

```python
from __future__ import annotations

import math
from typing import Any

from pydantic import BaseModel

from guardrail.models import ShadowState
from guardrail.projection import ProjectionError
```

**为什么必须迁**：Task 10 之后 `api/tools.py` 要 import 策略引擎，策略引擎要 import 派生参数计算；不迁就形成 `api.tools → policy.engine → api.tools` 的循环导入。派生参数计算是投影的一部分，本来就不该住在 API 层。

- [x] **Step 4: 改 `tests/test_projection_args.py` 的导入**

第 3 行：

```python
from guardrail.projection_args import PlannedAction, derive_projection_args
```

（原 `from guardrail.api.tools import ...` 改为从新模块导入。`build_shadow` 那行不变。）

- [x] **Step 5: 跑测试确认通过**

Run: `uv run pytest tests/test_projection_args.py -v`
Expected: 7 passed

- [x] **Step 6: 写 `src/guardrail/shadow_loader.py`**

```python
from __future__ import annotations

from typing import Any

import httpx

from guardrail.models import ShadowState
from guardrail.preview_rules import assert_action_applicable
from guardrail.projection import ProjectionError, build_shadow, project
from guardrail.projection_args import PlannedAction, derive_projection_args
from guardrail.tools.registry import TOOL_SPECS

_PREVIEW_ID_PREFIX = "preview-"


class ShadowLoadError(Exception):
    """装载影子状态失败。预览映射 400，结果层上限映射 deny。"""


def is_preview_id(value: object) -> bool:
    """是否为计划内部合成的占位 id（`preview-coupon-{step}` / `preview-order-{step}`）。

    真实商城的 id 有各自前缀（coupon 为 `c-…`、order 为 `o-…`），`preview-` 前缀
    只用于投影时给计划内新建实体占位，不可能与真实 id 撞车。装载器据此把
    「计划内引用」与「引用商城已有实体」区分开。
    """
    return str(value).startswith(_PREVIEW_ID_PREFIX)


async def load_referenced_entities(
    shop: httpx.AsyncClient, actions: list[PlannedAction]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把动作 args 引用的 coupon_id / order_id 装进影子状态。

    引用了商城里不存在的实体时直接失败——那是一份引用了不存在实体的计划，
    绝不能让审批人看到一份「看起来没问题」的投影终态。
    """
    coupon_ids: set[str] = set()
    order_ids: set[str] = set()
    for action in actions:
        coupon_id = action.args.get("coupon_id")
        if isinstance(coupon_id, str) and coupon_id and not is_preview_id(coupon_id):
            coupon_ids.add(coupon_id)
        order_id = action.args.get("order_id")
        if order_id and not is_preview_id(order_id):
            order_ids.add(str(order_id))

    coupons: list[dict[str, Any]] = []
    for coupon_id in sorted(coupon_ids):
        resp = await shop.get(f"/shop/v1/coupons/{coupon_id}")
        if resp.status_code == 404:
            raise ShadowLoadError(f"引用的优惠券不存在: {coupon_id}")
        resp.raise_for_status()
        coupons.append(resp.json())

    orders: list[dict[str, Any]] = []
    for order_id in sorted(order_ids):
        resp = await shop.get(f"/shop/v1/orders/{order_id}")
        if resp.status_code == 404:
            raise ShadowLoadError(f"引用的订单不存在: {order_id}")
        resp.raise_for_status()
        orders.append(resp.json())

    return coupons, orders


async def load_shadow(
    shop: httpx.AsyncClient, calls: list[tuple[str, dict[str, Any]]]
) -> ShadowState:
    """为一批调用装载影子状态：全部商品 + 它们引用到的券与订单。"""
    products = (await shop.get("/shop/v1/products")).json()
    actions = [PlannedAction(tool=tool, args=args) for tool, args in calls]
    coupons, orders = await load_referenced_entities(shop, actions)
    return build_shadow(products=products, coupons=coupons, orders=orders)


async def project_call(
    shop: httpx.AsyncClient, tool: str, args: dict[str, Any]
) -> ShadowState:
    """装载影子状态并投影一次调用，返回投影后的状态。

    逐步投影的基准是**当前影子状态**：同一商品在一个序列里被改价多次时，
    第二次的基准必须是第一次投影后的价格，否则投影与真实执行的终态对不上。
    """
    before = await load_shadow(shop, [(tool, args)])
    derived = derive_projection_args(PlannedAction(tool=tool, args=args), before, 0)
    assert_action_applicable(tool, derived, before)
    return project(before, TOOL_SPECS, [(tool, derived)])
```

- [x] **Step 7: 写 `src/guardrail/policy/caps.py`**

```python
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from guardrail.models import OrderSnapshot
from guardrail.policy.single import SingleCallPolicy
from guardrail.projection import ProjectionError
from guardrail.shadow_loader import ShadowLoadError, load_shadow, project_call

CAP_ERROR_PREFIX = "结果态上限"


class CapsEvaluationError(Exception):
    """结果层上限无法求值。调用方必须 fail-closed 拒绝。"""


# 可读字段的闭集，与 policy/lint.py 的 CAP_FIELDS 一一对应。
# 刻意不把表达式交给策略文件：能读任意字段就等于把求值沙箱的口子开在配置层。
_CAP_READERS: dict[str, Callable[[OrderSnapshot], int]] = {
    "total_amount_cents": lambda order: order.unit_price_cents * order.qty,
}


def _new_order(before_ids: set[str], after: Any) -> OrderSnapshot:
    """取出本次调用新建的订单。

    刻意要求「恰好一个」：0 个说明投影没生效，多个说明这次调用建了不止一张
    单——两种情况都意味着读到的金额不可信，必须拒绝而不是猜一个。
    """
    new_orders = [o for oid, o in after.orders.items() if oid not in before_ids]
    if len(new_orders) != 1:
        raise CapsEvaluationError(
            f"结果态上限无法求值：本次调用新建了 {len(new_orders)} 张订单，预期恰好 1 张"
        )
    return new_orders[0]


async def evaluate_result_caps(
    *,
    tool: str,
    args: dict[str, Any],
    policy: SingleCallPolicy,
    shop: httpx.AsyncClient,
) -> tuple[str, str] | None:
    """对一次调用做结果层上限校验。返回 `(规则 id, 原因)` 或 `None`。

    金额只从投影结果态读（spec §6.3）：模型可以谎报 args 里的任何数字，
    谎报不了投影结果。这也意味着**每个结果层规则都要付一次装载 + 投影的代价**，
    所以不要把所有规则都升级成结果层——只有「金额 / 总量 / 终态」这类约束才值得。
    """
    rules = [r for r in policy.rules_for(tool) if r.cap_field is not None]
    if not rules:
        return None

    try:
        before = await load_shadow(shop, [(tool, args)])
        after = await project_call(shop, tool, args)
    except (ShadowLoadError, ProjectionError) as exc:
        # 投影不出来就不能声称「没超限」。这是 fail-closed 最容易漏掉的一处：
        # 把它当成「通过」等于给了一条绕过上限的路径。
        raise CapsEvaluationError(
            f"{CAP_ERROR_PREFIX}无法求值，已按 fail-closed 拒绝：{exc}"
        ) from exc

    for rule in rules:
        reader = _CAP_READERS[rule.cap_field]
        amount = reader(_new_order(set(before.orders), after))
        assert rule.max is not None
        if amount > rule.max:
            return rule.id, rule.message
    return None
```

- [x] **Step 8: 改 `src/guardrail/api/tools.py`：删掉已迁出的代码，改用新模块**

1. 删掉 `PlannedAction`、`derive_projection_args`、`_require_float_arg`、`_require_int_arg`、`_require_optional_str_arg`、`_PREVIEW_ID_PREFIX`、`_is_preview_id`、`_load_referenced_entities`、`derive_projection_args` 相关的 `import math`。
2. import 段改为：

```python
from guardrail.models import ProjectedState, ShadowState, compute_metrics
from guardrail.projection import ProjectionError
from guardrail.projection_args import PlannedAction
from guardrail.shadow_loader import ShadowLoadError, load_referenced_entities
from guardrail.tools.handlers import ToolContext, handle
from guardrail.tools.registry import TOOL_SPECS
```

3. `PlanPreview` 里的 `actions: list[PlannedAction]` 不变（`PlannedAction` 从新模块来）。
4. `preview_plan` 里把
   `coupons, orders = await _load_referenced_entities(shop, body.actions)` 改成
   `try: coupons, orders = await load_referenced_entities(shop, body.actions) except ShadowLoadError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc`
5. `preview_plan` 里逐步投影的三行（`derive_projection_args` / `assert_action_applicable` / `project`）**保留原样**——预览的逐步投影与 `project_call` 的单次投影是两条不同用途的代码路径（前者要按 step 合成占位 id 并累积指标，后者只求一次调用的结果态）。相应地补上 import：`from guardrail.preview_rules import assert_action_applicable`、`from guardrail.projection import build_shadow, project`、`from guardrail.projection_args import derive_projection_args`。

- [x] **Step 9: 把第 7 步接进判定链**

`src/guardrail/policy/engine.py`：把 import 段补上

```python
from guardrail.policy.caps import CapsEvaluationError, evaluate_result_caps
```

并把 `evaluate_single_call` 的结尾（第 6 步之后）改为：

```python
    # 7 结果态上限（spec §6.3）。没有结果层规则时这一步不做任何投影。
    try:
        cap_hit = await evaluate_result_caps(
            tool=tool, args=args, policy=policy, shop=shop
        )
    except CapsEvaluationError as exc:
        return _deny(str(exc))
    if cap_hit is not None:
        rule_id, message = cap_hit
        return _deny(message, rule_id=rule_id)

    return SingleVerdict(decision="allow")
```

- [x] **Step 10: 跑测试确认通过**

Run: `uv run pytest tests/test_caps.py tests/test_single_policy.py tests/test_projection_args.py tests/test_gateway_api.py -v`
Expected: 全部通过

- [x] **Step 11: Commit**

```bash
git add src/guardrail/projection_args.py src/guardrail/shadow_loader.py \
  src/guardrail/policy/caps.py src/guardrail/api/tools.py src/guardrail/policy/engine.py \
  tests/test_caps.py tests/test_projection_args.py
git commit -m "feat: 结果态上限——金额从投影终态读取，不信 args；影子装载器从预览路径抽出共用"
```

---

### Task 10: 网关接线——判定 → 幂等 → 审计 → 执行

**Files:**
- Modify: `src/guardrail/api/tools.py`
- Modify: `src/guardrail/main.py`
- Modify: `tests/test_gateway_api.py`

**Interfaces:**
- Consumes: Task 4–9 的全部产出
- Produces: 重写后的 `POST /v1/tools/{name}`，响应体 `{"tool", "decision", "result", "reasons", "replayed"}`

- [x] **Step 1: 改 `tests/test_gateway_api.py` 的 fixture 与 agent**

把 fixture 拆成两层，让测试能同时拿到 client 与 app（审计故障注入需要换掉 `app.state.audit`）：

```python
@pytest.fixture
async def gateway(tmp_path):
    # 两个 app 都需要各自的 lifespan：商城要建表/播种，网关要建会话库与 httpx 客户端。
    shop_app = create_shop_app(str(tmp_path / "shop.db"))
    async with LifespanManager(shop_app):
        settings = Settings(
            shop_base_url="http://shop.test",
            gateway_db_path=str(tmp_path / "gateway.db"),
        )
        app = create_app(settings, shop_transport=httpx.ASGITransport(app=shop_app))
        async with LifespanManager(app):
            yield app


@pytest.fixture
async def client(gateway):
    transport = httpx.ASGITransport(app=gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
```

删除原来的 `client` fixture 与 `from tests.conftest import app_client` 这行 import（LifespanManager 已在用）。

把 `_new_session` 改成：

```python
async def _new_session(client, agent_id: str = "ops_agent", task_id: str = "t-1") -> str:
    r = await client.post("/v1/sessions", json={"agent_id": agent_id, "task_id": task_id})
    return r.json()["session_id"]


async def _prime(client, session_id: str) -> None:
    """读一次商品列表，把全部商品 id 登记进 provenance。

    判定链第 3 步要求写操作的目标实体必须在本会话读到过（spec §8），所以每个
    要写商品的测试都得先「看一眼」。这不是测试麻烦，是被测系统在按设计工作。
    """
    r = await client.post("/v1/tools/list_products", json={"session_id": session_id, "args": {}})
    assert r.status_code == 200
```

- [x] **Step 2: 列出必须改的既有测试**

| 测试 | 改动 | 原因 |
|---|---|---|
| `test_tool_call_end_to_end` | 调 `update_price` 前加 `await _prime(client, sid)` | provenance |
| `test_tool_call_update_price_non_finite_returns_409` | 拆成两条：`"nan"` / `"inf"` 期望 **400**，`1e305` 期望 **409** | 参数契约现在先于商城拦下非数值字符串 |
| `test_plan_preview_loads_referenced_order` | 调 `create_order` 前加 `await _prime(client, sid)` | provenance |
| `test_tool_call_missing_required_arg_returns_400` | 不变（参数畸形仍是 400） | — |
| `test_unknown_tool_returns_404` | 不变 | — |
| 其余 `test_plan_preview_*` | 不变（预览路径本计划不接判定链） | — |

`test_tool_call_update_price_non_finite_returns_409` 替换为：

```python
@pytest.mark.parametrize("delta_pct", ["nan", "inf"])
async def test_tool_call_update_price_non_numeric_is_rejected_by_schema(client, delta_pct):
    # 判定链第 4 步（参数契约）现在先于商城拦下非数值字符串：400 + 点名参数，
    # 比让请求打到商城再拿回一个 409 更早、也更好读。
    sid = await _new_session(client)
    await _prime(client, sid)
    r = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": delta_pct}},
    )
    assert r.status_code == 400
    assert "delta_pct" in r.json()["detail"]


async def test_tool_call_update_price_overflow_still_reaches_shop(client):
    # 1e305 是合法 JSON number，参数契约放行；由商城的 InvalidStateError 映射成 409。
    sid = await _new_session(client)
    await _prime(client, sid)
    r = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": 1e305}},
    )
    assert r.status_code == 409
    assert "delta_pct" in r.json()["detail"]
```

`test_tool_call_end_to_end` 改为：

```python
async def test_tool_call_end_to_end(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    r = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -10.0}},
    )
    assert r.status_code == 200
    assert r.json()["result"]["after"]["list_price_cents"] == round(599900 * 0.9)
    assert r.json()["decision"] == "allow"
    assert r.json()["replayed"] is False
```

`test_plan_preview_loads_referenced_order` 里在 `sid = await _new_session(client)` 之后加 `await _prime(client, sid)`。

- [x] **Step 3: 重写 `call_tool`**

`src/guardrail/api/tools.py` 的 `call_tool` 替换为：

```python
@router.post("/tools/{name}")
async def call_tool(request: Request, name: str, body: ToolCall) -> dict[str, Any]:
    """一次工具调用的完整网关路径。

    顺序（spec §5 路径 1）：
        1. 加载会话（不存在 → 404）
        2. 单次判定链：会话 / 白名单 / Provenance / 参数契约 / 权限 / 阈值 / 结果态上限
        3. 幂等：已完成 → 原样返回首次响应；进行中 → 409
        4. **写审计（先于执行）**
        5. 执行；失败则释放幂等键并把商城错误透传
        6. 登记 provenance、完成幂等、写执行结果审计

    两处与 spec 的偏离，都是实现顺序上的必然，写在这里以免后来者「修正」回去：

    - **幂等被拆成执行前后两步**（spec §6.1 把它列为判定链第 8 步）。它必须
      横跨「审计 → 执行 → 记账」，塞不进一条纯前置的判定链。快速路径（已完成）
      放在判定之前，避免为一个已知会重放的调用白跑一遍投影；占位登记放在执行
      之前，用一次条件写把并发重放挡在 409。计划 ③ 接上风险预算后，预算扣减
      必须放在占位登记之后、重放分支之前——否则一次重放会扣两次预算。
    - **放行会写两条审计**：一条「批准并即将执行」，一条「执行完成/失败」。
      spec §7 要求审计先于执行（这样不存在「执行了但没记上」的窗口），而执行
      结果只有执行后才知道。链是只追加的，所以补第二条而不是改第一条。
    """
    state = request.app.state
    record = await state.sessions.load(body.session_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {body.session_id}")

    if name not in TOOL_SPECS:
        # 未知工具也要进审计：被污染的模型开始试探工具名，正是最早期的信号。
        await _audit(state, body.session_id, name, body.args, "deny", [f"未知工具: {name}"])
        raise HTTPException(status_code=404, detail=f"未知工具: {name}")

    key = idempotency_key(body.session_id, name, body.args)
    cached = await state.idempotency.get(key)
    if cached is not None and cached.status == "done" and cached.response is not None:
        await _audit(
            state, body.session_id, name, body.args, "allow",
            ["幂等重放：返回首次响应，未重新执行"],
            replay=True,
        )
        return {**cached.response, "replayed": True}

    verdict = await evaluate_single_call(
        record=record,
        tool=name,
        args=body.args,
        policy=state.policy,
        provenance_store=state.provenance,
        shop=state.shop,
    )
    if verdict.decision == "deny":
        await _audit(state, body.session_id, name, body.args, "deny", verdict.reasons)
        status = 400 if verdict.kind == "invalid_args" else 403
        raise HTTPException(status_code=status, detail="; ".join(verdict.reasons))

    if not await state.idempotency.begin(key, body.session_id):
        await _audit(
            state, body.session_id, name, body.args, "deny",
            ["同一调用正在执行中，请稍后重试"],
        )
        raise HTTPException(status_code=409, detail="同一调用正在执行中，请稍后重试")

    await _audit_or_deny(state, body.session_id, name, body.args, "allow", verdict.reasons)

    try:
        result = await handle(name, body.args, ToolContext(shop=state.shop))
    except httpx.HTTPStatusError as exc:
        await state.idempotency.release(key)
        await _audit(
            state, body.session_id, name, body.args, "allow",
            [*(verdict.reasons or []), f"执行失败：{exc.response.status_code}"],
        )
        raise HTTPException(
            status_code=exc.response.status_code, detail=exc.response.text
        ) from exc
    except KeyError as exc:
        # handle 直接下标读取必需参数，缺参抛 KeyError。畸形输入必须是 400，
        # 而不是让 KeyError 逃逸成 500（商城自身的 404/409 仍由 HTTPStatusError 透传）。
        await state.idempotency.release(key)
        await _audit(
            state, body.session_id, name, body.args, "allow",
            [*(verdict.reasons or []), f"执行失败：缺少必需参数 {exc.args[0]!r}"],
        )
        raise HTTPException(
            status_code=400, detail=f"工具 {name!r} 缺少必需参数 {exc.args[0]!r}"
        ) from exc

    # provenance 登记放在执行之后：它记的是「这个实体确实被本会话产出过」。
    # 若这里失败，执行其实已经生效，不能当作「没发生」——但也不会发生：
    # 审计写入（上面）已经证明数据库可写。
    await register_result(state.provenance, body.session_id, TOOL_SPECS[name], result)

    response = {
        "tool": name,
        "decision": "allow",
        "result": result,
        "reasons": verdict.reasons,
    }
    await state.idempotency.complete(key, response)
    await _audit(
        state, body.session_id, name, body.args, "allow",
        [*(verdict.reasons or []), "执行完成"],
    )
    state.bus.publish(
        DecisionEvent(
            session_id=body.session_id,
            tool=name,
            decision="allow",
            reasons=verdict.reasons,
            timestamp=now_iso(),
        )
    )
    return {**response, "replayed": False}


async def _audit(
    state: Any,
    session_id: str,
    tool: str,
    args: dict[str, Any],
    decision: str,
    reasons: list[str],
    *,
    replay: bool = False,
) -> None:
    await state.audit.append(
        AuditDraft(
            session_id=session_id,
            tool=tool,
            args=args,
            decision=decision,  # type: ignore[arg-type]
            replay=replay,
            reasons=reasons,
            timestamp=now_iso(),
        )
    )


async def _audit_or_deny(
    state: Any,
    session_id: str,
    tool: str,
    args: dict[str, Any],
    decision: str,
    reasons: list[str],
) -> None:
    """审计必须先于执行；写不进去就取消执行（spec §7 / §10.2）。"""
    try:
        await _audit(state, session_id, tool, args, decision, reasons)
    except Exception as exc:
        raise HTTPException(
            status_code=403, detail=f"审计链写入失败，已取消执行：{exc}"
        ) from exc
```

import 段补上：

```python
from guardrail.audit import AuditDraft
from guardrail.clock import now_iso
from guardrail.idempotency import idempotency_key
from guardrail.policy.engine import evaluate_single_call
from guardrail.protocols import DecisionEvent
from guardrail.provenance import register_result
```

- [x] **Step 4: 改 `src/guardrail/main.py`**

```python
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from guardrail.api import audit, sessions, tools
from guardrail.config import Settings, get_settings
from guardrail.policy.loader import load_policy
from guardrail.policy.single import PolicyError
from guardrail.stores.audit import SqliteAuditSink
from guardrail.stores.idempotency import SqliteIdempotencyStore
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


def create_app(
    settings: Settings | None = None,
    shop_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """shop_transport 仅用于测试：把商城 app 挂在内存里，免起真实进程。"""
    resolved = settings or get_settings()

    # 策略在 app 构造期加载，不在 lifespan 里：一个没加载上策略的网关应当在
    # 起进程时就死掉，而不是变成一个「看起来在跑、其实不拦任何东西」的服务。
    try:
        policy = load_policy(resolved.policy_path)
    except PolicyError as exc:
        raise RuntimeError(f"策略加载失败，拒绝启动：{exc}") from exc
    assert_specs_valid()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        backend = SqliteBackend(resolved.gateway_db_path)
        await backend.connect()
        app.state.backend = backend
        app.state.sessions = SqliteSessionStore(backend)
        app.state.audit = SqliteAuditSink(backend)
        app.state.idempotency = SqliteIdempotencyStore(backend)
        app.state.provenance = SqliteProvenanceStore(backend)
        app.state.bus = InProcessDecisionEventBus()
        app.state.policy = policy
        app.state.shop = httpx.AsyncClient(
            base_url=resolved.shop_base_url, transport=shop_transport
        )
        yield
        await app.state.shop.aclose()
        await backend.close()

    app = FastAPI(title="会话级风险护栏", lifespan=lifespan)
    app.include_router(sessions.router)
    app.include_router(tools.router)
    app.include_router(audit.router)
    return app


app = create_app()
```

- [x] **Step 5: 追加端到端判定链测试**

在 `tests/test_gateway_api.py` 末尾追加：

```python
# ---------- 判定链接线 ----------

async def test_denied_call_is_not_executed(client):
    # spec §13 场景 1：Agent 试图把 iPhone 打一折 → 立即拒绝。
    sid = await _new_session(client)
    await _prime(client, sid)
    r = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -90.0}},
    )
    assert r.status_code == 403
    assert "10%" in r.json()["detail"]
    shop = (await client.post(
        "/v1/tools/get_product",
        json={"session_id": sid, "args": {"product_id": "p-iphone"}},
    )).json()["result"]["product"]
    assert shop["list_price_cents"] == 599900


async def test_denied_call_is_audited(gateway, client):
    sid = await _new_session(client)
    await _prime(client, sid)
    await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -90.0}},
    )
    entries = await gateway.state.audit.list_entries(sid)
    denied = [e for e in entries if e.decision == "deny"]
    assert len(denied) == 1
    assert "10%" in denied[0].reasons[0]


async def test_audit_chain_verifies_after_traffic(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -5.0}},
    )
    await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -90.0}},
    )
    verdict = (await client.get("/v1/audit/verify")).json()
    # 5 条 = list_products 的「批准 + 完成」+ update_price 成功的「批准 + 完成」
    # + 被拒的那 1 条。放行写两条是 spec §7「审计先于执行」的必然结果。
    assert verdict == {"ok": True, "checked": 5, "broken_at_seq": None, "reason": None}


async def test_unknown_tool_is_audited(gateway, client):
    sid = await _new_session(client)
    r = await client.post("/v1/tools/teleport", json={"session_id": sid, "args": {}})
    assert r.status_code == 404
    entries = await gateway.state.audit.list_entries(sid)
    assert any(e.tool == "teleport" and e.decision == "deny" for e in entries)


async def test_permission_denied_returns_403(client):
    sid = await _new_session(client, agent_id="risk_auditor")
    r = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -1.0}},
    )
    assert r.status_code == 403
    assert "无权" in r.json()["detail"]


async def test_provenance_denied_returns_403(client):
    sid = await _new_session(client)
    # 没有先读商品，p-iphone 就不在 provenance 里。
    r = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -1.0}},
    )
    assert r.status_code == 403
    assert "Provenance" in r.json()["detail"]


async def test_result_state_cap_denied_end_to_end(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    r = await client.post(
        "/v1/tools/create_order",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "qty": 1}},
    )
    assert r.status_code == 403
    assert "500.00" in r.json()["detail"]


async def test_idempotent_replay_returns_first_response(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    body = {"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -5.0}}
    first = await client.post("/v1/tools/update_price", json=body)
    second = await client.post("/v1/tools/update_price", json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["result"] == first.json()["result"]
    # 关键：只降了一次。599900 → 569905，若执行两次会是 539910。
    assert (await client.post(
        "/v1/tools/get_product",
        json={"session_id": sid, "args": {"product_id": "p-iphone"}},
    )).json()["result"]["product"]["list_price_cents"] == 569905


async def test_replay_is_audited_with_replay_flag(gateway, client):
    sid = await _new_session(client)
    await _prime(client, sid)
    body = {"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -5.0}}
    await client.post("/v1/tools/update_price", json=body)
    await client.post("/v1/tools/update_price", json=body)
    entries = await gateway.state.audit.list_entries(sid)
    assert any(e.replay is True for e in entries)


async def test_different_args_are_not_replays(client):
    sid = await _new_session(client)
    await _prime(client, sid)
    await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -5.0}},
    )
    second = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -6.0}},
    )
    assert second.json()["replayed"] is False


async def test_failed_execution_releases_idempotency_key(client):
    # 第一次因库存不足失败 → 幂等键必须被释放，否则这次调用在会话 TTL 内
    # 永远重试不了。
    sid = await _new_session(client)
    await _prime(client, sid)
    body = {"session_id": sid, "args": {"product_id": "p-iphone", "delta": -99999}}
    first = await client.post("/v1/tools/update_stock", json=body)
    assert first.status_code == 409
    body["args"]["delta"] = -1
    second = await client.post("/v1/tools/update_stock", json=body)
    assert second.status_code == 200


async def test_audit_failure_cancels_execution(gateway, client):
    # spec §10.2：审计写失败 → 拒绝。这是「执行了但没记上」那个窗口的守门人。
    class BrokenAudit:
        async def append(self, _draft):
            raise RuntimeError("磁盘满了")

    gateway.state.audit = BrokenAudit()
    sid = await _new_session(client)
    r = await client.post(
        "/v1/tools/get_product",
        json={"session_id": sid, "args": {"product_id": "p-iphone"}},
    )
    assert r.status_code == 403
    assert "审计" in r.json()["detail"]


async def test_successful_call_writes_two_audit_entries(gateway, client):
    sid = await _new_session(client)
    await _prime(client, sid)
    await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -5.0}},
    )
    update_entries = [
        e for e in await gateway.state.audit.list_entries(sid) if e.tool == "update_price"
    ]
    # list_entries 是 ORDER BY seq DESC，所以 [0] 是后写的那条。
    # 「批准并即将执行」+「执行完成」——审计先于执行是 spec §7 的硬要求。
    assert len(update_entries) == 2
    assert update_entries[0].reasons == ["执行完成"]
    assert update_entries[1].reasons == []


async def test_expired_session_is_rejected(client, gateway):
    sid = await _new_session(client)
    record = await gateway.state.sessions.load(sid)
    assert record is not None
    record.expires_at = "2000-01-01T00:00:00+00:00"
    await gateway.state.sessions.save(record)
    r = await client.post("/v1/tools/list_products", json={"session_id": sid, "args": {}})
    assert r.status_code == 403
    assert "过期" in r.json()["detail"]


async def test_session_creation_sweeps_expired(client, gateway):
    stale = await _new_session(client)
    record = await gateway.state.sessions.load(stale)
    assert record is not None
    record.expires_at = "2000-01-01T00:00:00+00:00"
    await gateway.state.sessions.save(record)
    await _new_session(client)
    assert await gateway.state.sessions.load(stale) is None
```

- [x] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_gateway_api.py -v`
Expected: 全部通过

- [x] **Step 7: 跑全量测试与 lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: 全部通过，无 lint 错误

- [x] **Step 8: Commit**

```bash
git add src/guardrail/api/tools.py src/guardrail/main.py tests/test_gateway_api.py
git commit -m "feat: 调用路径接线——判定→幂等→审计（先于执行）→执行→记账，审计失败即取消"
```

---

### Task 11: 端到端验收、spec 同步与文档

**Files:**
- Modify: `docs/superpowers/specs/01-session-risk-guardrail-design.md`
- Create: `README.md`（最小骨架，M11 再补全）

**Interfaces:**
- Consumes: Task 1–10 的全部产出
- Produces: 无新代码

- [x] **Step 1: 跑全量测试与 lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: 全部通过，无 lint 错误

- [x] **Step 2: 手工验收——复现 spec §13 场景 1**

终端一：

```bash
make shop
```

终端二：

```bash
make clean && make gateway
```

Expected: 网关启动日志无异常（策略加载成功）。若 `policies/single_call.yaml` 缺失或非法，网关应当**直接启动失败**并打印 `策略加载失败，拒绝启动：…`。

终端三：

```bash
SID=$(curl -s -X POST localhost:8000/v1/sessions \
  -H 'content-type: application/json' \
  -d '{"agent_id":"ops_agent","task_id":"t-demo"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
echo "session=$SID"
```

读一次商品（建立 provenance）：

```bash
curl -s -X POST localhost:8000/v1/tools/list_products \
  -H 'content-type: application/json' -d "{\"session_id\":\"$SID\",\"args\":{}}" | head -c 120
```

Expected: 200，商品列表。

把 iPhone 打一折 —— 应当被拒：

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/v1/tools/update_price \
  -H 'content-type: application/json' \
  -d "{\"session_id\":\"$SID\",\"args\":{\"product_id\":\"p-iphone\",\"delta_pct\":-90}}"
```

Expected: `403`

确认真实商城**未被改动**：

```bash
curl -s localhost:8100/shop/v1/products/p-iphone
```

Expected: `list_price_cents` 仍为 `599900`

- [x] **Step 3: 手工验收——幂等**

```bash
BODY="{\"session_id\":\"$SID\",\"args\":{\"product_id\":\"p-iphone\",\"delta_pct\":-5}}"
curl -s -X POST localhost:8000/v1/tools/update_price -H 'content-type: application/json' -d "$BODY" | head -c 200; echo
curl -s -X POST localhost:8000/v1/tools/update_price -H 'content-type: application/json' -d "$BODY" | head -c 200; echo
curl -s localhost:8100/shop/v1/products/p-iphone
```

Expected: 第二次响应含 `"replayed": true`；商品价格是 `569905`（只降一次），不是 `539910`。

- [x] **Step 4: 手工验收——审计链可验证**

```bash
curl -s localhost:8000/v1/audit/verify
```

Expected: `{"ok":true,"checked":N,"broken_at_seq":null,"reason":null}`，N ≥ 4

- [x] **Step 5: 手工验收——fail-closed 三条**

```bash
# ① 策略文件缺失 → 网关拒绝启动
mv policies/single_call.yaml /tmp/single_call.yaml.bak
make gateway   # 期望：启动失败，打印「策略加载失败，拒绝启动」
mv /tmp/single_call.yaml.bak policies/single_call.yaml
```

```bash
# ② 未列出的 agent → 全部拒绝
NEWSID=$(curl -s -X POST localhost:8000/v1/sessions \
  -H 'content-type: application/json' -d '{"agent_id":"ghost_agent"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_id"])')
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/v1/tools/list_products \
  -H 'content-type: application/json' -d "{\"session_id\":\"$NEWSID\",\"args\":{}}"
```

Expected: `403`

```bash
# ③ 策略引用了不存在的工具 → lint 拦截 → 拒绝启动
cp policies/single_call.yaml /tmp/single_call.yaml.bak
printf '\n  - id: bad\n    match: {tool: teleport}\n    deny_if: "args.x > 1"\n    message: m\n' >> policies/single_call.yaml
make gateway   # 期望：启动失败，打印「未通过自检」与「teleport」
cp /tmp/single_call.yaml.bak policies/single_call.yaml
```

- [x] **Step 6: 同步 spec 的 7 处偏离**

在 `docs/superpowers/specs/01-session-risk-guardrail-design.md` 做以下修改，每处都写清「为什么偏离」，不要只改结论：

1. **§6.1 判定链**：在判定链代码块下方补一段，说明两处顺序偏离——provenance 与白名单互换的原因（provenance 要读工具自己声明的 `requires`），以及幂等被拆到执行前后两步的原因（它必须横跨审计与执行）。同时把「8. 幂等」这一行标注为「由 API 层在执行前后完成，不在判定链内」。

2. **§6.2 幂等**：把「审计链记一条 `decision: replay`」改为「审计链记一条 `replay: true` 的记录，`decision` 仍为 `allow`」，并写明理由：`decision` 会被 `/metrics` 按值计数（§19.4），掺入非判定值会污染该指标。

3. **§6.2 幂等键**：把 `sha256(session_id ‖ tool ‖ canonical_json(args))` 改为 `sha256(canonical_json({session_id, tool, args}))`，写明分隔符碰撞的理由（`("ab","c")` 与 `("a","bc")`）。

4. **§6.3 结果态上限**：把示例里的 `cap_field: total_amount` / `max: 50000` 明确为 `cap_field: total_amount_cents` / `max: 50000`（整数分，= 500.00 元），并补一句：本计划只实现 `create_order` 一条结果层规则，其余上限留在语法层——理由是 §6.3 末尾那句「不要把所有规则都升级成投影求值」。

5. **§7 审计链**：在「顺序保证」段落补一句：放行会写**两条**记录（批准并即将执行 / 执行完成或失败），因为审计必须先于执行，而执行结果只有执行后才知道；链是只追加的，所以补第二条而不是改第一条。

6. **§8 Provenance 门**：把「工具返回值里出现的实体 ID 自动登记」改为「工具在网关侧清单里用 `emits` 声明产出路径、`requires` 声明前置依赖，登记与校验都按声明走」，并写明理由：递归扫描返回值会把商品名、邮箱域名也登记成实体，那道门形同虚设。

7. **§15 数据模型**：`sessions` 表补 `expires_at TEXT NOT NULL` 与 `idx_sessions_expires`；`audit_log` 的 `decision` 行补一句「不含 replay，replay 单独成列」。

改完校验：

Run: `grep -n "replay\|total_amount_cents\|expires_at" docs/superpowers/specs/01-session-risk-guardrail-design.md`
Expected: 至少 6 行命中

- [x] **Step 7: 写 `README.md` 骨架**

只写现在能兑现的部分，其余标注待补。**不要写任何未实测的数字**——拦截率与误伤率属于 M7。

```markdown
# agent-guardrail

会话级风险护栏网关。**其他护栏管「这一下能不能做」，本项目管「这一串做完会怎样」。**

一个被保护的迷你商城 + 一个护栏网关。Agent 只能通过网关操作商城，永远拿不到
商城的直接凭证。

## 现在能跑什么

```bash
make shop      # 终端一：迷你商城（仅内网）
make gateway   # 终端二：护栏网关
```

```bash
# 建会话
curl -X POST localhost:8000/v1/sessions \
  -H 'content-type: application/json' -d '{"agent_id":"ops_agent","task_id":"t-demo"}'

# 先读一眼（Provenance 门要求写操作的目标必须在本会话读到过）
curl -X POST localhost:8000/v1/tools/list_products \
  -H 'content-type: application/json' -d '{"session_id":"<sid>","args":{}}'

# 把 iPhone 打一折 → 403
curl -X POST localhost:8000/v1/tools/update_price \
  -H 'content-type: application/json' \
  -d '{"session_id":"<sid>","args":{"product_id":"p-iphone","delta_pct":-90}}'

# 审计链完整性
curl localhost:8000/v1/audit/verify
```

## 已实现的能力

| 层 | 能力 | 章节 |
|---|---|---|
| 标配 | 单次策略（YAML 策略即数据、受限表达式） | spec §6.1 |
| 标配 | 结果态上限（金额从投影终态读，不信 args） | spec §6.3 |
| 标配 | 审计哈希链（含篡改检测） | spec §7 |
| 标配 | Provenance 门 | spec §8 |
| 标配 | 幂等 | spec §6.2 |

## 还没实现

组合风险与风险预算、计划级审批、控制台、真实 Agent、三方基线评测。
里程碑见 spec §17。

## 设计文档

- [设计文档](../specs/01-session-risk-guardrail-design.md)
- [同类方案调研](../../research/01-agent-guardrail-competitive-analysis.md)

## 已知局限

哈希链只证明「记录未被篡改」，**不证明「记录内容在写入时为真」**；`tests/test_audit_chain.py`
里那个改库的后门就是这一事实的证据。完整局限清单见 spec §19.6（`docs/limitations.md`
随 M11 交付）。
```

- [x] **Step 8: Commit**

```bash
git add README.md docs/superpowers/specs/01-session-risk-guardrail-design.md
git commit -m "docs: 同步 spec 的 7 处实现偏离；README 骨架只写已兑现的能力"
```

---

## 完成标志

本计划完成时，以下必须全部成立：

- [x] `uv run pytest -v` 全绿
- [x] `uv run ruff check .` 无错误
- [x] spec §13 场景 1 可复现：Agent 把 iPhone 打一折 → 403 + 理由含「单次降价不得超过 10%」，且**真实商城价格未变**
- [x] 重复同一调用返回 `replayed: true`，商城只被改一次
- [x] `GET /v1/audit/verify` 返回 `ok: true`，且有流量之后仍为 true
- [x] 三条 fail-closed 全部可复现：策略文件缺失 → 拒绝启动；未列出 agent → 403；策略引用不存在工具 → 拒绝启动
- [x] `tests/test_audit_chain.py` 的 4 条篡改用例全部失败检测成功
- [x] 判定链的每一步都有对应测试，`STEP_ORDER` 被断言

## 本计划明确不做的

- **组合风险与风险预算**（spec §3）→ 计划 ③。接上时注意：预算扣减必须放在幂等占位登记**之后**、重放分支**之前**，否则一次重放会扣两次预算
- **计划生命周期与审批**（spec §4）→ 计划 ④
- **控制台**（spec §9）→ 计划 ⑤
- **决策三态里的 `ask`**（spec §10.1）→ 计划 ③。`ask` 需要 `pending_approvals` 表与风险预算，本计划只产出 `allow` / `deny`；`AuditEntry.decision` 从第一天起就按四值定义，届时不需要改表
- **计划预览路径的策略预检**（spec §4.4 步骤 ①）→ 计划 ④。预览是纯计算、不碰商城，所以 M2 之后它仍不接判定链——这不是漏洞
- **每条工具都算一遍结果层上限**（spec §6.3）→ 本计划只做 `create_order` 金额上限一条。其余上限留在语法层，理由是 spec §6.3 自己的告诫
- **策略 lint CLI**（spec §19.5）→ 计划 ⑧。lint 逻辑已就位（`policy/lint.py`），缺的只是命令行入口
- **内存版存储**（spec §14 的 `stores/memory.py`）→ 计划 ⑤。控制台做出来时才需要
- **MCP / LangChain 适配器**（spec §19.3）→ 计划 ⑩
- **可观测性**（spec §19.4）→ 计划 ⑨。`DecisionEventBus` 协议与进程内实现已就位，M3 挂上 deny/ask 事件即可
