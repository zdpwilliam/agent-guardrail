# 商城与工具层 实现计划

> **交付状态：已实现并合入 v1.2.0。**
> **同步口径：本文件保留当时的实施步骤与代码快照，不作为当前文件结构、API
> 或测试数量的唯一来源；当前实现以 `src/`、`tests/`、系统设计和 README 为准。**
> **工具口径：本计划描述的是当时 9 个电商工具；当前注册表另有 7 个 corp 域工具。**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭出被保护对象（迷你商城）与护栏网关的工具层，使投影执行能算出计划终态并与真实执行结果一致。

**Architecture:** 两个进程。`shop/` 是可信的迷你商城，只监听内网端口，提供原子变更接口。`src/guardrail/` 是网关，持有工具清单与**效果声明**（网关侧数据，不是工具侧契约），并维护一份影子领域模型用于投影。本计划只做「执行 + 投影」，不做任何策略判定——策略在计划 ② 引入。

**Tech Stack:** Python 3.11+ / FastAPI / Pydantic v2 / aiosqlite / httpx / pytest + pytest-asyncio + hypothesis / uv / ruff

## Global Constraints

- Python 版本下限：`3.11`
- 金额一律用**整数分**（`*_cents: int`）。禁止用 float 存金额——浮点误差会让 spec §6.3 的金额上限在边界上失效
- 时间戳一律 ISO 8601 带时区文本，如 `2026-10-03T14:22:05+00:00`
- ruff 配置：`line-length = 100`，`select = ["E","F","I","UP","B","SIM","N","ANN"]`，`target-version = "py311"`
- 所有 HTTP 响应体为 Pydantic 模型的 JSON，不加自定义包络
- 本计划内**不实现**任何策略判定、审计链、provenance、会话风险状态——那些属于计划 ②③
- 工具数量固定 9 个，清单见 spec §16
- 商城只监听 `127.0.0.1`，网关是唯一调用方

---

## File Structure

| 文件 | 职责 |
|---|---|
| `pyproject.toml` | 依赖与工具配置 |
| `Makefile` | 开发命令入口 |
| `.env.example` | 配置模板 |
| `src/guardrail/config.py` | 环境变量 → Settings |
| `src/guardrail/models.py` | 全部 Pydantic 领域模型（快照、指标、效果声明、工具清单） |
| `src/guardrail/projection.py` | 影子状态 + 投影引擎 |
| `src/guardrail/tools/registry.py` | 9 个工具的定义与效果声明（网关侧数据） |
| `src/guardrail/tools/handlers.py` | 工具实现——调用商城 HTTP，不含任何护栏逻辑 |
| `src/guardrail/stores/sqlite.py` | 网关侧 SQLite 会话存储 |
| `src/guardrail/api/tools.py` | `POST /v1/tools/{name}`、`POST /v1/plans/preview` |
| `src/guardrail/api/sessions.py` | `POST /v1/sessions` |
| `src/guardrail/main.py` | 网关 FastAPI app factory |
| `shop/store.py` | 商城 SQLite 仓储，原子变更 |
| `shop/main.py` | 商城 FastAPI app |
| `tests/` | 见各任务 |

---

### Task 1: 项目脚手架与配置

**Files:**
- Create: `pyproject.toml`
- Create: `Makefile`
- Create: `.env.example`
- Create: `src/guardrail/__init__.py`（空）
- Create: `src/guardrail/config.py`
- Create: `tests/__init__.py`（空）
- Create: `tests/conftest.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: 无
- Produces: `guardrail.config.Settings`（字段 `shop_base_url: str`、`gateway_db_path: str`）、`guardrail.config.get_settings() -> Settings`（`lru_cache`）

- [x] **Step 1: 写 pyproject.toml**

```toml
[project]
name = "agent-guardrail"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "pydantic>=2.9",
    "aiosqlite>=0.20",
    "httpx>=0.27",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "hypothesis>=6.115",
    "asgi-lifespan>=2.1",
    "ruff>=0.7",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/guardrail", "shop"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "N", "ANN"]

# 测试与 fixture 不做类型标注——pytest 的 fixture 注入依赖未标注的参数名，
# 强行标注会让每个测试都多出一行噪音，而收益为零。源码仍强制 ANN。
[tool.ruff.lint.per-file-ignores]
"tests/**" = ["ANN"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
pythonpath = ["src", "."]
```

- [x] **Step 2: 写 `src/guardrail/config.py`**

```python
from functools import lru_cache

from pydantic import BaseModel


class Settings(BaseModel):
    shop_base_url: str = "http://127.0.0.1:8100"
    gateway_db_path: str = "data/gateway.db"


@lru_cache
def get_settings() -> Settings:
    """从环境变量加载配置。只读，进程生命周期内不变。"""
    import os

    return Settings(
        shop_base_url=os.getenv("SHOP_BASE_URL", "http://127.0.0.1:8100"),
        gateway_db_path=os.getenv("GATEWAY_DB_PATH", "data/gateway.db"),
    )
```

- [x] **Step 3: 写 `tests/conftest.py`**

```python
from contextlib import asynccontextmanager

import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.config import Settings


@asynccontextmanager
async def app_client(app):
    """把一个 ASGI app 包成 httpx.AsyncClient，并正确触发 lifespan。

    httpx 的 ASGITransport **不会**跑 lifespan 事件，所以必须用 LifespanManager
    包一层。否则 app.state.store 等不会被初始化，所有依赖它的测试都会失败。
    """
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            yield c


@pytest.fixture
def settings(tmp_path):
    return Settings(
        shop_base_url="http://testserver",
        gateway_db_path=str(tmp_path / "gateway.db"),
    )
```

- [x] **Step 4: 写失败测试 `tests/test_config.py`**

```python
from guardrail.config import Settings, get_settings


def test_settings_defaults():
    s = Settings()
    assert s.shop_base_url == "http://127.0.0.1:8100"


def test_get_settings_reads_env(monkeypatch):
    monkeypatch.setenv("SHOP_BASE_URL", "http://example.test:9999")
    get_settings.cache_clear()
    assert get_settings().shop_base_url == "http://example.test:9999"
    get_settings.cache_clear()
```

- [x] **Step 5: 装依赖并跑测试**

Run: `uv sync && uv run pytest tests/test_config.py -v`
Expected: 2 passed

- [x] **Step 6: 写 Makefile 与 `.env.example`**

```makefile
.PHONY: test lint fmt shop gateway

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
```

```
# 网关
SHOP_BASE_URL=http://127.0.0.1:8100
GATEWAY_DB_PATH=data/gateway.db
# 商城
SHOP_DB_PATH=data/shop.db
```

- [x] **Step 7: Commit**

```bash
git add pyproject.toml Makefile .env.example src/guardrail/__init__.py src/guardrail/config.py tests/__init__.py tests/conftest.py tests/test_config.py
git commit -m "chore: 项目脚手架与配置加载"
```

---

### Task 2: 领域模型与业务指标

**Files:**
- Create: `src/guardrail/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `ProductSnapshot(product_id: str, cost_price_cents: int, list_price_cents: int, stock: int)`
  - `CouponSnapshot(coupon_id: str, code: str, discount_pct: float, max_uses: int, used: int)`
  - `OrderSnapshot(order_id: str, product_id: str, qty: int, unit_price_cents: int, coupon_id: str | None, status: str)`
  - `ShadowState(products, coupons, orders, touched)`，方法 `clone() -> ShadowState`
  - `BusinessMetrics`（字段见下）
  - `ProjectedState(entities: dict[str, ProductSnapshot], metrics: BusinessMetrics, triggered_rules: list[str])`
  - `EffectOp(target: str, field: str, op: Literal["add","set","mul","append"], value: Any)`
  - `ToolSpec(name: str, kind: Literal["read","write"], effects: list[EffectOp], taint_source: bool, taint_sink: bool)`
  - `compute_metrics(before: ShadowState, after: ShadowState) -> BusinessMetrics`

- [x] **Step 1: 写失败测试 `tests/test_models.py`**

```python
from guardrail.models import (
    ProductSnapshot,
    ShadowState,
    compute_metrics,
)


def make_state(products):
    return ShadowState(products={p.product_id: p for p in products})


def test_compute_metrics_gross_margin_before_and_after():
    before = make_state(
        [
            ProductSnapshot(product_id="p1", cost_price_cents=6000, list_price_cents=10000, stock=10),
            ProductSnapshot(product_id="p2", cost_price_cents=4000, list_price_cents=10000, stock=10),
        ]
    )
    after = make_state(
        [
            ProductSnapshot(product_id="p1", cost_price_cents=6000, list_price_cents=5000, stock=10),
            ProductSnapshot(product_id="p2", cost_price_cents=4000, list_price_cents=10000, stock=10),
        ]
    )
    m = compute_metrics(before, after)
    # before: revenue=20000 cost=10000 -> 50.0
    assert m.gross_margin_pct_before == 50.0
    # after:  revenue=15000 cost=10000 -> 33.33
    assert m.gross_margin_pct_after == 33.33
    assert m.affected_product_count == 1
    assert m.avg_price_cents_before == 10000
    assert m.avg_price_cents_after == 7500
    assert m.cash_impact_cents == 0


def test_compute_metrics_no_change_is_identity():
    s = make_state(
        [ProductSnapshot(product_id="p1", cost_price_cents=6000, list_price_cents=10000, stock=1)]
    )
    m = compute_metrics(s, s.clone())
    assert m.gross_margin_pct_before == m.gross_margin_pct_after
    assert m.affected_product_count == 0
    assert m.cash_impact_cents == 0


def test_clone_is_deep():
    s = make_state(
        [ProductSnapshot(product_id="p1", cost_price_cents=6000, list_price_cents=10000, stock=1)]
    )
    c = s.clone()
    c.products["p1"].list_price_cents = 1
    assert s.products["p1"].list_price_cents == 10000
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'guardrail.models'`

- [x] **Step 3: 写 `src/guardrail/models.py`**

```python
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProductSnapshot(BaseModel):
    product_id: str
    cost_price_cents: int
    list_price_cents: int
    stock: int


class CouponSnapshot(BaseModel):
    coupon_id: str
    code: str
    discount_pct: float
    max_uses: int
    used: int


class OrderSnapshot(BaseModel):
    order_id: str
    product_id: str
    qty: int
    unit_price_cents: int
    coupon_id: str | None = None
    status: str = "created"


class ShadowState(BaseModel):
    """网关侧影子领域模型。投影只改它，永不碰真实商城。"""

    products: dict[str, ProductSnapshot] = Field(default_factory=dict)
    coupons: dict[str, CouponSnapshot] = Field(default_factory=dict)
    orders: dict[str, OrderSnapshot] = Field(default_factory=dict)
    touched: set[str] = Field(default_factory=set)

    def clone(self) -> "ShadowState":
        return ShadowState(
            products={k: v.model_copy(deep=True) for k, v in self.products.items()},
            coupons={k: v.model_copy(deep=True) for k, v in self.coupons.items()},
            orders={k: v.model_copy(deep=True) for k, v in self.orders.items()},
            touched=set(self.touched),
        )


class BusinessMetrics(BaseModel):
    gross_margin_pct_before: float
    gross_margin_pct_after: float
    avg_price_cents_before: int
    avg_price_cents_after: int
    affected_product_count: int
    cash_impact_cents: int


class ProjectedState(BaseModel):
    entities: dict[str, ProductSnapshot] = Field(default_factory=dict)
    metrics: BusinessMetrics
    triggered_rules: list[str] = Field(default_factory=list)


class EffectOp(BaseModel):
    """效果声明的一条操作。value 是模板串，形如 '{args.delta_pct}'。"""

    target: str
    field: str
    op: Literal["add", "set", "mul", "append"]
    value: Any = None


class ToolSpec(BaseModel):
    name: str
    kind: Literal["read", "write"]
    effects: list[EffectOp] = Field(default_factory=list)
    taint_source: bool = False
    taint_sink: bool = False


def _margin_pct(products: dict[str, ProductSnapshot]) -> float:
    revenue = sum(p.list_price_cents for p in products.values())
    cost = sum(p.cost_price_cents for p in products.values())
    if revenue == 0:
        return 0.0
    return round((revenue - cost) / revenue * 100, 2)


def _avg_price(products: dict[str, ProductSnapshot]) -> int:
    if not products:
        return 0
    return round(sum(p.list_price_cents for p in products.values()) / len(products))


def compute_metrics(before: ShadowState, after: ShadowState) -> BusinessMetrics:
    """从前后两个影子状态算出业务指标。只比较 before 中出现过的商品。"""
    affected = [
        pid
        for pid in before.products
        if pid in after.products
        and (
            before.products[pid].list_price_cents != after.products[pid].list_price_cents
            or before.products[pid].stock != after.products[pid].stock
        )
    ]
    new_orders = [o for oid, o in after.orders.items() if oid not in before.orders]
    cash_impact = sum(o.unit_price_cents * o.qty for o in new_orders)

    return BusinessMetrics(
        gross_margin_pct_before=_margin_pct(before.products),
        gross_margin_pct_after=_margin_pct(after.products),
        avg_price_cents_before=_avg_price(before.products),
        avg_price_cents_after=_avg_price(after.products),
        affected_product_count=len(affected),
        cash_impact_cents=cash_impact,
    )
```

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_models.py -v`
Expected: 3 passed

- [x] **Step 5: Commit**

```bash
git add src/guardrail/models.py tests/test_models.py
git commit -m "feat: 领域模型、影子状态与业务指标计算"
```

---

### Task 3: 商城存储层与建表

**Files:**
- Create: `shop/__init__.py`（空）
- Create: `shop/store.py`
- Modify: `docs/superpowers/specs/01-session-risk-guardrail-design.md`（§15 金额字段改整数分）
- Test: `tests/test_shop_store.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `shop.store.ShopStore(db_path: str)`，方法 `async connect()`、`async close()`、`async init_schema()`、`async seed_demo_data()`
  - `ShopStore` 读方法：`async get_product(product_id) -> ProductRow | None`、`async list_products(category=None) -> list[ProductRow]`
  - `ProductRow`：dataclass，字段 `id, name, category, cost_price_cents, list_price_cents, stock`

- [x] **Step 1: 写失败测试 `tests/test_shop_store.py`**

```python
import pytest

from shop.store import ShopStore


@pytest.fixture
async def store(tmp_path):
    s = ShopStore(str(tmp_path / "shop.db"))
    await s.connect()
    await s.init_schema()
    yield s
    await s.close()


async def test_init_schema_creates_tables(store):
    rows = await store._fetchall("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    assert {"products", "orders", "coupons"} <= {r["name"] for r in rows}


async def test_money_columns_are_integer_cents(store):
    cols = await store._fetchall("PRAGMA table_info(products)")
    types = {c["name"]: c["type"] for c in cols}
    assert types["cost_price_cents"] == "INTEGER"
    assert types["list_price_cents"] == "INTEGER"


async def test_seed_and_list_products(store):
    await store.seed_demo_data()
    products = await store.list_products()
    assert len(products) >= 6
    p = await store.get_product(products[0].id)
    assert p is not None and p.cost_price_cents > 0


async def test_list_products_by_category(store):
    await store.seed_demo_data()
    summer = await store.list_products(category="夏季款")
    assert len(summer) >= 4
    assert all(p.category == "夏季款" for p in summer)
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_shop_store.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'shop.store'`

- [x] **Step 3: 写 `shop/store.py`（本步只到 schema 与读方法）**

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT NOT NULL,
  cost_price_cents INTEGER NOT NULL,
  list_price_cents INTEGER NOT NULL,
  stock INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS coupons (
  id TEXT PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  discount_pct REAL NOT NULL,
  max_uses INTEGER NOT NULL,
  used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  product_id TEXT NOT NULL,
  qty INTEGER NOT NULL,
  unit_price_cents INTEGER NOT NULL,
  coupon_id TEXT,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""

DEMO_PRODUCTS = [
    ("p-iphone", "iPhone 15", "数码", 480000, 599900, 40),
    ("p-ipad", "iPad Air", "数码", 320000, 439900, 60),
    ("p-airpods", "AirPods Pro", "数码", 120000, 189900, 120),
    ("p-tshirt-s", "夏季T恤（S）", "夏季款", 3500, 9900, 300),
    ("p-tshirt-m", "夏季T恤（M）", "夏季款", 3500, 9900, 280),
    ("p-shorts", "夏季短裤", "夏季款", 4200, 12900, 150),
    ("p-sandals", "夏季凉鞋", "夏季款", 5000, 15900, 90),
    ("p-cap", "夏季遮阳帽", "夏季款", 2800, 7900, 200),
]


@dataclass(frozen=True)
class ProductRow:
    id: str
    name: str
    category: str
    cost_price_cents: int
    list_price_cents: int
    stock: int


class ShopStore:
    """商城仓储。所有写操作都是单条原子的 UPDATE/INSERT。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("ShopStore.connect() 未调用")
        return self._conn

    async def init_schema(self) -> None:
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def seed_demo_data(self) -> None:
        await self.conn.executemany(
            "INSERT OR REPLACE INTO products"
            " (id, name, category, cost_price_cents, list_price_cents, stock)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            DEMO_PRODUCTS,
        )
        await self.conn.commit()

    async def get_product(self, product_id: str) -> ProductRow | None:
        rows = await self._fetchall("SELECT * FROM products WHERE id = ?", (product_id,))
        return _to_product(rows[0]) if rows else None

    async def list_products(self, category: str | None = None) -> list[ProductRow]:
        if category is None:
            rows = await self._fetchall("SELECT * FROM products ORDER BY id")
        else:
            rows = await self._fetchall(
                "SELECT * FROM products WHERE category = ? ORDER BY id", (category,)
            )
        return [_to_product(r) for r in rows]


def _to_product(row: aiosqlite.Row) -> ProductRow:
    return ProductRow(
        id=row["id"],
        name=row["name"],
        category=row["category"],
        cost_price_cents=row["cost_price_cents"],
        list_price_cents=row["list_price_cents"],
        stock=row["stock"],
    )
```

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_shop_store.py -v`
Expected: 4 passed

- [x] **Step 5: 同步更新 spec §15 的金额字段**

把 spec `docs/superpowers/specs/01-session-risk-guardrail-design.md` §15 里的 `products` 表定义改成：

```sql
CREATE TABLE products (
  id TEXT PRIMARY KEY, name TEXT, category TEXT,
  cost_price_cents INTEGER, list_price_cents INTEGER, stock INTEGER
);
```

在 §15 的 SQL 块下方补一句：

```
金额一律以**整数分**存储，禁止用浮点。理由：§6.3 的金额上限在边界上依赖精确比较，
浮点误差会让上限时灵时不灵。
```

改完校验：

Run: `grep -n "price_cents" docs/superpowers/specs/01-session-risk-guardrail-design.md`
Expected: 至少 1 行命中（两个金额字段写在同一行 SQL 里）

- [x] **Step 6: Commit**

```bash
git add shop/__init__.py shop/store.py tests/test_shop_store.py docs/superpowers/specs/01-session-risk-guardrail-design.md
git commit -m "feat: 商城存储层与建表；金额改用整数分并同步 spec"
```

---

### Task 4: 商城写操作（原子变更）

**Files:**
- Modify: `shop/store.py`（追加异常、dataclass 与写方法）
- Test: `tests/test_shop_writes.py`

**Interfaces:**
- Consumes: `ShopStore`（Task 3）
- Produces（均为 `ShopStore` 方法）：
  - `async update_price(product_id: str, delta_pct: float) -> ProductRow`
  - `async update_stock(product_id: str, delta: int) -> ProductRow`
  - `async create_coupon(code: str, discount_pct: float, max_uses: int) -> CouponRow`
  - `async get_coupon(coupon_id: str) -> CouponRow | None`
  - `async create_order(product_id: str, qty: int, coupon_id: str | None = None) -> OrderRow`
  - `async get_order(order_id: str) -> OrderRow | None`
  - `async refund_order(order_id: str) -> OrderRow`
  - 异常 `ShopError`、`NotFoundError`、`InvalidStateError`；dataclass `CouponRow`、`OrderRow`

- [x] **Step 1: 写失败测试 `tests/test_shop_writes.py`**

```python
import pytest

from shop.store import InvalidStateError, NotFoundError, ShopStore


@pytest.fixture
async def store(tmp_path):
    s = ShopStore(str(tmp_path / "shop.db"))
    await s.connect()
    await s.init_schema()
    await s.seed_demo_data()
    yield s
    await s.close()


async def test_update_price_applies_delta(store):
    before = await store.get_product("p-iphone")
    after = await store.update_price("p-iphone", -10.0)
    assert after.list_price_cents == round(before.list_price_cents * 0.9)


async def test_update_price_unknown_product_raises(store):
    with pytest.raises(NotFoundError):
        await store.update_price("p-nope", -10.0)


async def test_update_stock_rejects_negative_result(store):
    with pytest.raises(InvalidStateError):
        await store.update_stock("p-iphone", -99999)


async def test_create_coupon_and_use_it(store):
    coupon = await store.create_coupon("SUMMER20", 20.0, 100)
    product = await store.get_product("p-tshirt-s")
    order = await store.create_order("p-tshirt-s", 2, coupon.id)
    assert order.unit_price_cents == round(product.list_price_cents * 0.8)
    refreshed = await store.get_coupon(coupon.id)
    assert refreshed.used == 1


async def test_coupon_exhausted_raises(store):
    coupon = await store.create_coupon("ONCE", 10.0, 1)
    await store.create_order("p-tshirt-s", 1, coupon.id)
    with pytest.raises(InvalidStateError):
        await store.create_order("p-tshirt-s", 1, coupon.id)


async def test_refund_order_twice_raises(store):
    order = await store.create_order("p-tshirt-s", 1, None)
    await store.refund_order(order.id)
    with pytest.raises(InvalidStateError):
        await store.refund_order(order.id)
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_shop_writes.py -v`
Expected: FAIL —— `ImportError: cannot import name 'NotFoundError'`

- [x] **Step 3: 在 `shop/store.py` 补 import 与异常/dataclass**

文件顶部改成：

```python
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
```

在 `ProductRow` 定义之后加：

```python
class ShopError(Exception):
    """商城领域错误的基类。"""


class NotFoundError(ShopError):
    pass


class InvalidStateError(ShopError):
    pass


@dataclass(frozen=True)
class CouponRow:
    id: str
    code: str
    discount_pct: float
    max_uses: int
    used: int


@dataclass(frozen=True)
class OrderRow:
    id: str
    product_id: str
    qty: int
    unit_price_cents: int
    coupon_id: str | None
    status: str
    created_at: str
```

- [x] **Step 4: 在 `ShopStore` 类内追加写方法（`list_products` 之后，`_to_product` 之前）**

```python
    async def update_price(self, product_id: str, delta_pct: float) -> ProductRow:
        product = await self.get_product(product_id)
        if product is None:
            raise NotFoundError(f"商品不存在: {product_id}")
        new_price = round(product.list_price_cents * (1 + delta_pct / 100))
        if new_price <= 0:
            raise InvalidStateError(f"改价后价格非正: {new_price}")
        await self.conn.execute(
            "UPDATE products SET list_price_cents = ? WHERE id = ?", (new_price, product_id)
        )
        await self.conn.commit()
        updated = await self.get_product(product_id)
        if updated is None:
            raise NotFoundError(f"商品不存在: {product_id}")
        return updated

    async def update_stock(self, product_id: str, delta: int) -> ProductRow:
        product = await self.get_product(product_id)
        if product is None:
            raise NotFoundError(f"商品不存在: {product_id}")
        new_stock = product.stock + delta
        if new_stock < 0:
            raise InvalidStateError(f"库存不能为负: {new_stock}")
        await self.conn.execute(
            "UPDATE products SET stock = ? WHERE id = ?", (new_stock, product_id)
        )
        await self.conn.commit()
        updated = await self.get_product(product_id)
        if updated is None:
            raise NotFoundError(f"商品不存在: {product_id}")
        return updated

    async def create_coupon(self, code: str, discount_pct: float, max_uses: int) -> CouponRow:
        if not 0 < discount_pct < 100:
            raise InvalidStateError(f"折扣率越界: {discount_pct}")
        coupon_id = f"c-{uuid.uuid4().hex[:12]}"
        await self.conn.execute(
            "INSERT INTO coupons (id, code, discount_pct, max_uses, used) VALUES (?, ?, ?, ?, 0)",
            (coupon_id, code, discount_pct, max_uses),
        )
        await self.conn.commit()
        coupon = await self.get_coupon(coupon_id)
        if coupon is None:
            raise NotFoundError(f"优惠券创建后读取失败: {coupon_id}")
        return coupon

    async def get_coupon(self, coupon_id: str) -> CouponRow | None:
        rows = await self._fetchall("SELECT * FROM coupons WHERE id = ?", (coupon_id,))
        if not rows:
            return None
        r = rows[0]
        return CouponRow(
            id=r["id"],
            code=r["code"],
            discount_pct=r["discount_pct"],
            max_uses=r["max_uses"],
            used=r["used"],
        )

    async def create_order(
        self, product_id: str, qty: int, coupon_id: str | None = None
    ) -> OrderRow:
        if qty <= 0:
            raise InvalidStateError(f"数量必须为正: {qty}")
        product = await self.get_product(product_id)
        if product is None:
            raise NotFoundError(f"商品不存在: {product_id}")
        unit_price = product.list_price_cents
        if coupon_id is not None:
            coupon = await self.get_coupon(coupon_id)
            if coupon is None:
                raise NotFoundError(f"优惠券不存在: {coupon_id}")
            if coupon.used >= coupon.max_uses:
                raise InvalidStateError(f"优惠券已用尽: {coupon_id}")
            unit_price = round(unit_price * (1 - coupon.discount_pct / 100))
            await self.conn.execute("UPDATE coupons SET used = used + 1 WHERE id = ?", (coupon_id,))
        order_id = f"o-{uuid.uuid4().hex[:12]}"
        await self.conn.execute(
            "INSERT INTO orders"
            " (id, product_id, qty, unit_price_cents, coupon_id, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, 'created', ?)",
            (order_id, product_id, qty, unit_price, coupon_id, _now_iso()),
        )
        await self.conn.commit()
        order = await self.get_order(order_id)
        if order is None:
            raise NotFoundError(f"订单创建后读取失败: {order_id}")
        return order

    async def get_order(self, order_id: str) -> OrderRow | None:
        rows = await self._fetchall("SELECT * FROM orders WHERE id = ?", (order_id,))
        if not rows:
            return None
        r = rows[0]
        return OrderRow(
            id=r["id"],
            product_id=r["product_id"],
            qty=r["qty"],
            unit_price_cents=r["unit_price_cents"],
            coupon_id=r["coupon_id"],
            status=r["status"],
            created_at=r["created_at"],
        )

    async def refund_order(self, order_id: str) -> OrderRow:
        order = await self.get_order(order_id)
        if order is None:
            raise NotFoundError(f"订单不存在: {order_id}")
        if order.status != "created":
            raise InvalidStateError(f"订单状态不可退款: {order.status}")
        await self.conn.execute("UPDATE orders SET status = 'refunded' WHERE id = ?", (order_id,))
        await self.conn.commit()
        updated = await self.get_order(order_id)
        if updated is None:
            raise NotFoundError(f"订单不存在: {order_id}")
        return updated
```

- [x] **Step 5: 在文件末尾加时间辅助函数**

```python
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
```

- [x] **Step 6: 跑测试确认通过**

Run: `uv run pytest tests/test_shop_writes.py -v`
Expected: 6 passed

- [x] **Step 7: Commit**

```bash
git add shop/store.py tests/test_shop_writes.py
git commit -m "feat: 商城写操作，含库存/券/退款的状态校验"
```

---

### Task 5: 商城内部 HTTP 服务

**Files:**
- Create: `shop/config.py`
- Create: `shop/main.py`
- Test: `tests/test_shop_api.py`

**Interfaces:**
- Consumes: `ShopStore`、`ProductRow`、`CouponRow`、`OrderRow`、错误家族（Task 3、4）
- Produces: `shop.main.create_app(db_path: str | None = None) -> FastAPI`，路由：
  - `GET  /shop/v1/products?category=`
  - `GET  /shop/v1/products/{product_id}`
  - `POST /shop/v1/products/{product_id}/price` body `{"delta_pct": float}`
  - `POST /shop/v1/products/{product_id}/stock` body `{"delta": int}`
  - `POST /shop/v1/coupons` body `{"code","discount_pct","max_uses"}`
  - `GET  /shop/v1/coupons/{coupon_id}`
  - `POST /shop/v1/orders` body `{"product_id","qty","coupon_id"}`
  - `GET  /shop/v1/orders/{order_id}`
  - `POST /shop/v1/orders/{order_id}/refund`
  - 错误映射：`NotFoundError → 404`，`InvalidStateError → 409`

- [x] **Step 1: 写失败测试 `tests/test_shop_api.py`**

```python
import pytest

from shop.main import create_app
from tests.conftest import app_client


@pytest.fixture
async def client(tmp_path):
    async with app_client(create_app(str(tmp_path / "shop.db"))) as c:
        yield c


async def test_list_products(client):
    r = await client.get("/shop/v1/products")
    assert r.status_code == 200
    assert len(r.json()) >= 6


async def test_get_product_404(client):
    r = await client.get("/shop/v1/products/p-nope")
    assert r.status_code == 404


async def test_update_price_roundtrip(client):
    before = (await client.get("/shop/v1/products/p-iphone")).json()
    r = await client.post("/shop/v1/products/p-iphone/price", json={"delta_pct": -10.0})
    assert r.status_code == 200
    assert r.json()["list_price_cents"] == round(before["list_price_cents"] * 0.9)


async def test_stock_negative_returns_409(client):
    r = await client.post("/shop/v1/products/p-iphone/stock", json={"delta": -99999})
    assert r.status_code == 409


async def test_coupon_and_order_flow(client):
    c = await client.post(
        "/shop/v1/coupons", json={"code": "S20", "discount_pct": 20.0, "max_uses": 5}
    )
    assert c.status_code == 200
    o = await client.post(
        "/shop/v1/orders",
        json={"product_id": "p-tshirt-s", "qty": 2, "coupon_id": c.json()["id"]},
    )
    assert o.status_code == 200
    assert o.json()["status"] == "created"


async def test_refund_twice_returns_409(client):
    o = await client.post(
        "/shop/v1/orders", json={"product_id": "p-tshirt-s", "qty": 1, "coupon_id": None}
    )
    oid = o.json()["id"]
    assert (await client.post(f"/shop/v1/orders/{oid}/refund")).status_code == 200
    assert (await client.post(f"/shop/v1/orders/{oid}/refund")).status_code == 409
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_shop_api.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'shop.main'`

- [x] **Step 3: 写 `shop/main.py`**

```python
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from shop.config import get_shop_settings
from shop.store import InvalidStateError, NotFoundError, ShopStore


class PriceDelta(BaseModel):
    delta_pct: float


class StockDelta(BaseModel):
    delta: int


class CouponCreate(BaseModel):
    code: str
    discount_pct: float
    max_uses: int


class OrderCreate(BaseModel):
    product_id: str
    qty: int
    coupon_id: str | None = None


class Product(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    category: str
    cost_price_cents: int
    list_price_cents: int
    stock: int


class Coupon(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    discount_pct: float
    max_uses: int
    used: int


class Order(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    product_id: str
    qty: int
    unit_price_cents: int
    coupon_id: str | None
    status: str
    created_at: str


def create_app(db_path: str | None = None) -> FastAPI:
    resolved_db = db_path or get_shop_settings().shop_db_path

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = ShopStore(resolved_db)
        await store.connect()
        await store.init_schema()
        await store.seed_demo_data()
        app.state.store = store
        yield
        await store.close()

    app = FastAPI(title="迷你商城（内部）", lifespan=lifespan)

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(InvalidStateError)
    async def _invalid(_: Request, exc: InvalidStateError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/shop/v1/products", response_model=list[Product])
    async def list_products(request: Request, category: str | None = None) -> list[Product]:
        rows = await request.app.state.store.list_products(category)
        return [Product.model_validate(p) for p in rows]

    @app.get("/shop/v1/products/{product_id}", response_model=Product)
    async def get_product(request: Request, product_id: str) -> Product:
        p = await request.app.state.store.get_product(product_id)
        if p is None:
            raise NotFoundError(f"商品不存在: {product_id}")
        return Product.model_validate(p)

    @app.post("/shop/v1/products/{product_id}/price", response_model=Product)
    async def update_price(request: Request, product_id: str, body: PriceDelta) -> Product:
        return Product.model_validate(
            await request.app.state.store.update_price(product_id, body.delta_pct)
        )

    @app.post("/shop/v1/products/{product_id}/stock", response_model=Product)
    async def update_stock(request: Request, product_id: str, body: StockDelta) -> Product:
        return Product.model_validate(
            await request.app.state.store.update_stock(product_id, body.delta)
        )

    @app.post("/shop/v1/coupons", response_model=Coupon)
    async def create_coupon(request: Request, body: CouponCreate) -> Coupon:
        return Coupon.model_validate(
            await request.app.state.store.create_coupon(body.code, body.discount_pct, body.max_uses)
        )

    @app.get("/shop/v1/coupons/{coupon_id}", response_model=Coupon)
    async def get_coupon(request: Request, coupon_id: str) -> Coupon:
        c = await request.app.state.store.get_coupon(coupon_id)
        if c is None:
            raise NotFoundError(f"优惠券不存在: {coupon_id}")
        return Coupon.model_validate(c)

    @app.post("/shop/v1/orders", response_model=Order)
    async def create_order(request: Request, body: OrderCreate) -> Order:
        return Order.model_validate(
            await request.app.state.store.create_order(body.product_id, body.qty, body.coupon_id)
        )

    @app.get("/shop/v1/orders/{order_id}", response_model=Order)
    async def get_order(request: Request, order_id: str) -> Order:
        o = await request.app.state.store.get_order(order_id)
        if o is None:
            raise NotFoundError(f"订单不存在: {order_id}")
        return Order.model_validate(o)

    @app.post("/shop/v1/orders/{order_id}/refund", response_model=Order)
    async def refund_order(request: Request, order_id: str) -> Order:
        return Order.model_validate(await request.app.state.store.refund_order(order_id))

    return app


app = create_app()
```

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_shop_api.py -v`
Expected: 8 passed

- [x] **Step 5: Commit**

```bash
git add shop/main.py tests/test_shop_api.py
git commit -m "feat: 商城内部 HTTP 服务与领域错误映射"
```

---

### Task 6: 影子领域模型与投影引擎

**Files:**
- Create: `src/guardrail/projection.py`
- Test: `tests/test_projection.py`

**Interfaces:**
- Consumes: `ShadowState`、`EffectOp`、`ToolSpec`（Task 2）
- Produces:
  - `resolve_template(value: Any, args: dict) -> Any`
  - `apply_effect(state: ShadowState, op: EffectOp, args: dict) -> None`（原地修改）
  - `project(state: ShadowState, tools: dict[str, ToolSpec], calls: list[tuple[str, dict]]) -> ShadowState`
  - `build_shadow(products: list[dict], coupons: list[dict], orders: list[dict]) -> ShadowState`

- [x] **Step 1: 写失败测试 `tests/test_projection.py`**

```python
import pytest

from guardrail.models import EffectOp, ToolSpec
from guardrail.projection import (
    ProjectionError,
    apply_effect,
    build_shadow,
    project,
    resolve_template,
)


def state_with_price(price_cents=10000, stock=10):
    return build_shadow(
        products=[
            {
                "id": "p1",
                "name": "T",
                "category": "c",
                "cost_price_cents": 6000,
                "list_price_cents": price_cents,
                "stock": stock,
            }
        ],
        coupons=[],
        orders=[],
    )


def test_resolve_template_pulls_from_args():
    assert resolve_template("{args.delta_pct}", {"delta_pct": -10}) == -10
    assert resolve_template("literal", {"delta_pct": -10}) == "literal"


def test_apply_effect_add_mutates_in_place():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.absolute_delta_cents}",
    )
    apply_effect(s, op, {"product_id": "p1", "absolute_delta_cents": -1000})
    assert s.products["p1"].list_price_cents == 9000
    assert "product:p1" in s.touched


def test_project_single_price_change_does_not_mutate_input():
    s = state_with_price()
    tools = {
        "update_price": ToolSpec(
            name="update_price",
            kind="write",
            effects=[
                EffectOp(
                    target="product:{args.product_id}",
                    field="list_price_cents",
                    op="add",
                    value="{args.absolute_delta_cents}",
                )
            ],
        )
    }
    out = project(
        s, tools, [("update_price", {"product_id": "p1", "absolute_delta_cents": -1000})]
    )
    assert out.products["p1"].list_price_cents == 9000
    assert s.products["p1"].list_price_cents == 10000


def test_project_unknown_tool_raises():
    with pytest.raises(KeyError):
        project(state_with_price(), {}, [("nope", {})])


def test_apply_effect_set_status_on_order():
    s = build_shadow(
        products=[],
        coupons=[],
        orders=[
            {
                "id": "o1",
                "product_id": "p1",
                "qty": 1,
                "unit_price_cents": 100,
                "coupon_id": None,
                "status": "created",
            }
        ],
    )
    op = EffectOp(target="order:{args.order_id}", field="status", op="set", value="refunded")
    apply_effect(s, op, {"order_id": "o1"})
    assert s.orders["o1"].status == "refunded"


def test_apply_effect_append_coupon():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}")
    apply_effect(s, op, {"coupon_id": "c1", "code": "X", "discount_pct": 20.0, "max_uses": 5})
    assert s.coupons["c1"].discount_pct == 20.0
    assert "coupon:c1" in s.touched


def test_apply_effect_missing_entity_raises():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.absolute_delta_cents}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p-missing", "absolute_delta_cents": -1000})


def test_apply_effect_unknown_field_raises():
    s = state_with_price()
    op = EffectOp(target="product:{args.product_id}", field="nonexistent", op="set", value="x")
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1"})


def test_apply_effect_unresolved_template_variable_raises():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.missing}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1"})


def test_apply_effect_unknown_entity_type_raises():
    s = state_with_price()
    op = EffectOp(target="widget:{args.widget_id}", field="stock", op="set", value=5)
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"widget_id": "w1"})


def test_touched_not_recorded_when_effect_raises():
    s = state_with_price()
    op = EffectOp(target="product:{args.product_id}", field="nonexistent", op="set", value="x")
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1"})
    assert "product:p1" not in s.touched


def test_project_mid_sequence_raise_leaves_input_untouched():
    s = state_with_price()
    tools = {
        "update_price": ToolSpec(
            name="update_price",
            kind="write",
            effects=[
                EffectOp(
                    target="product:{args.product_id}",
                    field="list_price_cents",
                    op="add",
                    value="{args.absolute_delta_cents}",
                )
            ],
        ),
        "break_it": ToolSpec(
            name="break_it",
            kind="write",
            effects=[
                EffectOp(
                    target="product:{args.product_id}",
                    field="nonexistent",
                    op="set",
                    value="x",
                )
            ],
        ),
    }
    calls = [
        ("update_price", {"product_id": "p1", "absolute_delta_cents": -1000}),
        ("break_it", {"product_id": "p1"}),
    ]
    before = s.model_copy(deep=True)
    with pytest.raises(ProjectionError):
        project(s, tools, calls)
    assert s == before


def test_apply_effect_empty_entity_id_raises():
    s = state_with_price()
    op = EffectOp(target="product:", field="stock", op="set", value=1)
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {})


def test_apply_effect_add_non_numeric_value_raises():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.delta}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1", "delta": "not-a-number"})


def test_apply_effect_mul_non_numeric_field_raises():
    s = build_shadow(
        products=[],
        coupons=[],
        orders=[
            {
                "id": "o1",
                "product_id": "p1",
                "qty": 1,
                "unit_price_cents": 100,
                "coupon_id": None,
                "status": "created",
            }
        ],
    )
    op = EffectOp(target="order:{args.order_id}", field="status", op="mul", value=0.5)
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"order_id": "o1"})


def test_apply_effect_non_numeric_add_leaves_field_unchanged():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.delta}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1", "delta": "not-a-number"})
    assert s.products["p1"].list_price_cents == 10000


def test_apply_effect_non_numeric_mul_leaves_field_unchanged():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="mul",
        value="{args.factor}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1", "factor": "two"})
    assert s.products["p1"].list_price_cents == 10000


def test_apply_effect_append_coupon_missing_key_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}")
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"coupon_id": "c1", "code": "X", "max_uses": 5})


def test_apply_effect_append_coupon_bad_discount_pct_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}")
    with pytest.raises(ProjectionError):
        apply_effect(
            s, op, {"coupon_id": "c1", "code": "X", "discount_pct": "abc", "max_uses": 5}
        )


def test_apply_effect_append_order_missing_key_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="order:{args.order_id}", field="", op="append", value="{args.order_id}")
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"order_id": "o1", "product_id": "p1", "unit_price_cents": 100})


def test_apply_effect_append_order_fractional_cents_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="order:{args.order_id}", field="", op="append", value="{args.order_id}")
    with pytest.raises(ProjectionError):
        apply_effect(
            s,
            op,
            {"order_id": "o1", "product_id": "p1", "qty": 1, "unit_price_cents": 100.5},
        )


def test_apply_effect_add_fractional_cents_raises():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.delta}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1", "delta": -1000.5})
    assert s.products["p1"].list_price_cents == 10000


def test_apply_effect_add_integral_float_cents_coerced():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="add",
        value="{args.delta}",
    )
    apply_effect(s, op, {"product_id": "p1", "delta": -1000.0})
    assert s.products["p1"].list_price_cents == 9000
    assert isinstance(s.products["p1"].list_price_cents, int)


def test_apply_effect_set_fractional_cents_raises():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="set",
        value="{args.price}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1", "price": 12345.5})
    assert s.products["p1"].list_price_cents == 10000


def test_apply_effect_set_integral_float_cents_coerced():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="list_price_cents",
        op="set",
        value="{args.price}",
    )
    apply_effect(s, op, {"product_id": "p1", "price": 12345.0})
    assert s.products["p1"].list_price_cents == 12345
    assert isinstance(s.products["p1"].list_price_cents, int)


def test_apply_effect_add_non_integral_int_field_raises():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="stock",
        op="add",
        value="{args.delta}",
    )
    with pytest.raises(ProjectionError):
        apply_effect(s, op, {"product_id": "p1", "delta": -3.5})
    assert s.products["p1"].stock == 10


def test_apply_effect_add_integral_float_int_field_coerced():
    s = state_with_price()
    op = EffectOp(
        target="product:{args.product_id}",
        field="stock",
        op="add",
        value="{args.delta}",
    )
    apply_effect(s, op, {"product_id": "p1", "delta": -4.0})
    assert s.products["p1"].stock == 6
    assert isinstance(s.products["p1"].stock, int)


def test_apply_effect_add_float_field_keeps_float():
    s = build_shadow(
        products=[],
        coupons=[
            {
                "id": "c1",
                "code": "X",
                "discount_pct": 10.0,
                "max_uses": 5,
                "used": 0,
            }
        ],
        orders=[],
    )
    op = EffectOp(
        target="coupon:{args.coupon_id}",
        field="discount_pct",
        op="add",
        value="{args.delta}",
    )
    apply_effect(s, op, {"coupon_id": "c1", "delta": 5.5})
    assert s.coupons["c1"].discount_pct == 15.5


def test_apply_effect_append_coupon_bool_max_uses_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}")
    with pytest.raises(ProjectionError):
        apply_effect(
            s, op, {"coupon_id": "c1", "code": "X", "discount_pct": 20.0, "max_uses": True}
        )


def test_apply_effect_append_coupon_bool_discount_pct_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}")
    with pytest.raises(ProjectionError):
        apply_effect(
            s, op, {"coupon_id": "c1", "code": "X", "discount_pct": True, "max_uses": 5}
        )


def test_apply_effect_append_order_bool_qty_raises():
    s = build_shadow(products=[], coupons=[], orders=[])
    op = EffectOp(target="order:{args.order_id}", field="", op="append", value="{args.order_id}")
    with pytest.raises(ProjectionError):
        apply_effect(
            s,
            op,
            {"order_id": "o1", "product_id": "p1", "qty": True, "unit_price_cents": 100},
        )
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_projection.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'guardrail.projection'`

- [x] **Step 3: 写 `src/guardrail/projection.py`**

```python
from __future__ import annotations

import re
from typing import Any

from guardrail.models import (
    CouponSnapshot,
    EffectOp,
    OrderSnapshot,
    ProductSnapshot,
    ShadowState,
    ToolSpec,
)

_TEMPLATE_RE = re.compile(r"^\{args\.([a-zA-Z_][a-zA-Z0-9_]*)\}$")
_INLINE_RE = re.compile(r"\{args\.([a-zA-Z_][a-zA-Z0-9_]*)\}")


class ProjectionError(Exception):
    """投影无法应用某个效果时抛出；网关 API 会将其映射为 HTTP 400。"""


def resolve_template(value: object, args: dict[str, Any]) -> object:
    """'{args.x}' → args['x']；其他原样返回。"""
    if isinstance(value, str):
        m = _TEMPLATE_RE.match(value)
        if m:
            return args[m.group(1)]
    return value


def _template_vars(text: str) -> list[str]:
    """提取字符串里所有 '{args.xxx}' 模板变量名。"""
    return _INLINE_RE.findall(text)


def _resolve_target(target: str, args: dict[str, Any]) -> tuple[str, str]:
    """'product:{args.product_id}' → ('product', 'p1')。"""
    resolved = _INLINE_RE.sub(lambda m: str(args.get(m.group(1), "")), target)
    entity_type, _, entity_id = resolved.partition(":")
    return entity_type, entity_id


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_numeric(value: object, target: str, op: str) -> None:
    if not _is_number(value):
        raise ProjectionError(f"投影失败：效果 {target!r} 的 {op} 操作需要数值，但收到 {value!r}")


def _require_key(args: dict[str, Any], key: str, target: str) -> object:
    """取出 append 必需的参数；缺失时抛出 ProjectionError，而不是静默兜底。"""
    if key not in args:
        raise ProjectionError(f"投影失败：效果 {target!r} 的 append 缺少必需参数 {key!r}")
    return args[key]


def _coerce_str(value: object) -> str:
    return str(value)


def _coerce_float(value: object, key: str, target: str) -> float:
    if isinstance(value, bool):
        raise ProjectionError(
            f"投影失败：效果 {target!r} 的 append 参数 {key!r} 必须是浮点数，收到 {value!r}"
        )
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ProjectionError(
            f"投影失败：效果 {target!r} 的 append 参数 {key!r} 无法转换为浮点数，收到 {value!r}"
        ) from None


def _coerce_int(value: object, key: str, target: str) -> int:
    if isinstance(value, bool):
        raise ProjectionError(
            f"投影失败：效果 {target!r} 的 append 参数 {key!r} 必须是整数，收到 {value!r}"
        )
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ProjectionError(
            f"投影失败：效果 {target!r} 的 append 参数 {key!r} 无法转换为整数，收到 {value!r}"
        ) from None


def _coerce_integral(value: object, key: str, target: str, *, noun: str = "整数") -> int:
    """把整数字段的值收敛为 int：积分浮点（-4.0）转 int，非积分与 bool 抛错。"""
    if isinstance(value, bool):
        raise ProjectionError(f"投影失败：效果 {target!r} 的 {key!r} 必须是{noun}，收到 {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ProjectionError(
            f"投影失败：效果 {target!r} 的 {key!r} 必须是{noun}，收到非整数值 {value!r}"
        )
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ProjectionError(
            f"投影失败：效果 {target!r} 的 {key!r} 无法转换为{noun}，收到 {value!r}"
        ) from None


def _coerce_cents(value: object, key: str, target: str) -> int:
    """把 *_cents 字段的值收敛为整数分：积分浮点（-1000.0）转 int，非积分抛错。"""
    return _coerce_integral(value, key, target, noun="整数金额分")


def apply_effect(state: ShadowState, op: EffectOp, args: dict[str, Any]) -> None:
    """把一条效果声明应用到影子状态上（原地修改）。无法应用时抛出 ProjectionError。"""
    for var in _template_vars(op.target):
        if var not in args:
            raise ProjectionError(
                f"投影失败：效果 {op.target!r} 引用的模板变量 {var!r} 未在 args 中提供"
            )
    if isinstance(op.value, str):
        for var in _template_vars(op.value):
            if var not in args:
                raise ProjectionError(
                    f"投影失败：效果 {op.target!r} 引用的模板变量 {var!r} 未在 args 中提供"
                )

    entity_type, entity_id = _resolve_target(op.target, args)
    if not entity_id:
        raise ProjectionError(f"投影失败：效果 {op.target!r} 解析出的实体 id 为空")
    if entity_type not in ("product", "coupon", "order"):
        raise ProjectionError(f"投影失败：效果 {op.target!r} 的实体类型 {entity_type!r} 未知")
    value = resolve_template(op.value, args)

    if entity_type == "product":
        if op.op == "append":
            raise ProjectionError(f"投影失败：append 仅支持 coupon / order，收到 {op.target!r}")
        product = state.products.get(entity_id)
        if product is None:
            raise ProjectionError(
                f"投影失败：效果 {op.target!r} 的目标 product {entity_id!r} 不在影子状态中"
            )
        _apply_field(product, op.field, op.op, value, op.target)
    elif entity_type == "coupon":
        if op.op == "append":
            state.coupons[entity_id] = CouponSnapshot(
                coupon_id=entity_id,
                code=_coerce_str(_require_key(args, "code", op.target)),
                discount_pct=_coerce_float(
                    _require_key(args, "discount_pct", op.target), "discount_pct", op.target
                ),
                max_uses=_coerce_int(
                    _require_key(args, "max_uses", op.target), "max_uses", op.target
                ),
                used=0,
            )
        else:
            coupon = state.coupons.get(entity_id)
            if coupon is None:
                raise ProjectionError(
                    f"投影失败：效果 {op.target!r} 的目标 coupon {entity_id!r} 不在影子状态中"
                )
            _apply_field(coupon, op.field, op.op, value, op.target)
    else:  # order
        if op.op == "append":
            state.orders[entity_id] = OrderSnapshot(
                order_id=entity_id,
                product_id=_coerce_str(_require_key(args, "product_id", op.target)),
                qty=_coerce_int(_require_key(args, "qty", op.target), "qty", op.target),
                unit_price_cents=_coerce_cents(
                    _require_key(args, "unit_price_cents", op.target),
                    "unit_price_cents",
                    op.target,
                ),
                coupon_id=args.get("coupon_id"),
                status="created",
            )
        else:
            order = state.orders.get(entity_id)
            if order is None:
                raise ProjectionError(
                    f"投影失败：效果 {op.target!r} 的目标 order {entity_id!r} 不在影子状态中"
                )
            _apply_field(order, op.field, op.op, value, op.target)

    state.touched.add(f"{entity_type}:{entity_id}")


def _apply_field(obj: object, field: str, op: str, value: object, target: str) -> None:
    if not field or not hasattr(obj, field):
        raise ProjectionError(f"投影失败：效果 {target!r} 的字段 {field!r} 在目标对象上不存在")
    current = getattr(obj, field)
    if op == "add":
        _require_numeric(value, target, op)
        _require_numeric(current, target, op)
        result = current + value
        if field.endswith("_cents"):
            result = _coerce_cents(result, field, target)
        elif isinstance(current, int) and not isinstance(current, bool):
            result = _coerce_integral(result, field, target)
        setattr(obj, field, result)
    elif op == "set":
        if field.endswith("_cents"):
            value = _coerce_cents(value, field, target)
        setattr(obj, field, value)
    elif op == "mul":
        _require_numeric(value, target, op)
        _require_numeric(current, target, op)
        result = round(current * value)
        if field.endswith("_cents"):
            result = _coerce_cents(result, field, target)
        elif isinstance(current, int) and not isinstance(current, bool):
            result = _coerce_integral(result, field, target)
        setattr(obj, field, result)
    else:
        raise ProjectionError(f"投影失败：效果 {target!r} 使用了不支持的 op {op!r}")


def project(
    state: ShadowState, tools: dict[str, ToolSpec], calls: list[tuple[str, dict[str, Any]]]
) -> ShadowState:
    """在影子状态上按序执行动作序列。绝不修改传入的 state。"""
    shadow = state.clone()
    for tool_name, args in calls:
        spec = tools[tool_name]
        for op in spec.effects:
            apply_effect(shadow, op, args)
    return shadow


def build_shadow(
    products: list[dict[str, Any]], coupons: list[dict[str, Any]], orders: list[dict[str, Any]]
) -> ShadowState:
    """从商城的行数据构建影子状态。"""
    return ShadowState(
        products={
            p["id"]: ProductSnapshot(
                product_id=p["id"],
                cost_price_cents=p["cost_price_cents"],
                list_price_cents=p["list_price_cents"],
                stock=p["stock"],
            )
            for p in products
        },
        coupons={
            c["id"]: CouponSnapshot(
                coupon_id=c["id"],
                code=c["code"],
                discount_pct=c["discount_pct"],
                max_uses=c["max_uses"],
                used=c["used"],
            )
            for c in coupons
        },
        orders={
            o["id"]: OrderSnapshot(
                order_id=o["id"],
                product_id=o["product_id"],
                qty=o["qty"],
                unit_price_cents=o["unit_price_cents"],
                coupon_id=o["coupon_id"],
                status=o["status"],
            )
            for o in orders
        },
    )
```

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_projection.py -v`
Expected: 6 passed

- [x] **Step 5: Commit**

```bash
git add src/guardrail/projection.py tests/test_projection.py
git commit -m "feat: 网关侧影子领域模型与投影引擎"
```

---

### Task 7: 工具清单与效果声明

**Files:**
- Create: `src/guardrail/tools/__init__.py`（空）
- Create: `src/guardrail/tools/registry.py`
- Test: `tests/test_tool_registry.py`

**Interfaces:**
- Consumes: `ToolSpec`、`EffectOp`（Task 2）
- Produces: `TOOL_SPECS: dict[str, ToolSpec]`（固定 9 个工具）、`assert_specs_valid() -> None`

- [x] **Step 1: 写失败测试 `tests/test_tool_registry.py`**

```python
import pytest

from guardrail.models import EffectOp, ToolSpec
from guardrail.tools.registry import TOOL_SPECS, assert_specs_valid


def _specs_with_effect(effect: EffectOp) -> dict[str, ToolSpec]:
    specs = dict(TOOL_SPECS)
    specs["update_price"] = ToolSpec(name="update_price", kind="write", effects=[effect])
    return specs


def test_exactly_nine_tools():
    assert len(TOOL_SPECS) == 9


def test_expected_tool_names():
    assert set(TOOL_SPECS) == {
        "list_products",
        "get_product",
        "get_order",
        "update_price",
        "update_stock",
        "create_coupon",
        "create_order",
        "refund_order",
        "send_email",
    }


def test_read_tools_have_no_effects():
    for name in ("list_products", "get_product", "get_order"):
        assert TOOL_SPECS[name].effects == []


def test_write_tools_declare_expected_fields():
    assert {op.field for op in TOOL_SPECS["update_price"].effects} == {"list_price_cents"}
    assert {op.field for op in TOOL_SPECS["update_stock"].effects} == {"stock"}


def test_taint_source_and_sink_are_declared():
    assert TOOL_SPECS["get_order"].taint_source is True
    assert TOOL_SPECS["send_email"].taint_sink is True


def test_assert_specs_valid_passes_on_shipped_registry():
    assert_specs_valid()


@pytest.mark.parametrize("target", ["productx:{args.product_id}", ":x"])
def test_assert_specs_valid_rejects_unknown_entity_type(target: str):
    specs = _specs_with_effect(EffectOp(target=target, field="list_price_cents", op="add"))
    with pytest.raises(RuntimeError, match="实体类型未知"):
        assert_specs_valid(specs)


def test_assert_specs_valid_rejects_empty_entity_id():
    specs = _specs_with_effect(EffectOp(target="product:", field="list_price_cents", op="add"))
    with pytest.raises(RuntimeError, match="实体 id 为空"):
        assert_specs_valid(specs)


def test_assert_specs_valid_rejects_unknown_field():
    specs = _specs_with_effect(
        EffectOp(target="product:{args.product_id}", field="list_price_centss", op="add")
    )
    with pytest.raises(RuntimeError, match="字段"):
        assert_specs_valid(specs)


def test_assert_specs_valid_rejects_append_on_product():
    specs = _specs_with_effect(
        EffectOp(target="product:{args.product_id}", field="", op="append")
    )
    with pytest.raises(RuntimeError, match="append"):
        assert_specs_valid(specs)
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_tool_registry.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'guardrail.tools'`

- [x] **Step 3: 写 `src/guardrail/tools/registry.py`**

```python
from __future__ import annotations

from guardrail.models import (
    CouponSnapshot,
    EffectOp,
    OrderSnapshot,
    ProductSnapshot,
    ToolSpec,
)

# 效果声明是网关侧数据，不是工具的代码。
# 工具实现（handlers.py）不含任何护栏逻辑；接入新工具时只改这里。
TOOL_SPECS: dict[str, ToolSpec] = {
    "list_products": ToolSpec(name="list_products", kind="read"),
    "get_product": ToolSpec(name="get_product", kind="read"),
    "get_order": ToolSpec(name="get_order", kind="read", taint_source=True),
    "update_price": ToolSpec(
        name="update_price",
        kind="write",
        effects=[
            EffectOp(
                target="product:{args.product_id}",
                field="list_price_cents",
                op="add",
                value="{args.absolute_delta_cents}",
            )
        ],
    ),
    "update_stock": ToolSpec(
        name="update_stock",
        kind="write",
        effects=[
            EffectOp(
                target="product:{args.product_id}",
                field="stock",
                op="add",
                value="{args.delta}",
            )
        ],
    ),
    "create_coupon": ToolSpec(
        name="create_coupon",
        kind="write",
        effects=[
            EffectOp(
                target="coupon:{args.coupon_id}", field="", op="append", value="{args.coupon_id}"
            )
        ],
    ),
    "create_order": ToolSpec(
        name="create_order",
        kind="write",
        effects=[
            EffectOp(
                target="order:{args.order_id}", field="", op="append", value="{args.order_id}"
            ),
            EffectOp(
                target="coupon:{args.coupon_id}",
                field="used",
                op="add",
                value=1,
                optional=True,
            ),
        ],
    ),
    "refund_order": ToolSpec(
        name="refund_order",
        kind="write",
        effects=[
            EffectOp(target="order:{args.order_id}", field="status", op="set", value="refunded")
        ],
    ),
    "send_email": ToolSpec(name="send_email", kind="write", taint_sink=True),
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


def assert_specs_valid(specs: dict[str, ToolSpec] | None = None) -> None:
    """启动期自检。工具清单有问题时进程应当直接起不来。"""
    if specs is None:
        specs = TOOL_SPECS
    if len(specs) != 9:
        raise RuntimeError(f"工具数量应为 9，实际 {len(specs)}")
    for name, spec in specs.items():
        if spec.name != name:
            raise RuntimeError(f"工具键名与 spec.name 不一致: {name} != {spec.name}")
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
```

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_tool_registry.py -v`
Expected: 11 passed

- [x] **Step 5: Commit**

```bash
git add src/guardrail/tools/__init__.py src/guardrail/tools/registry.py tests/test_tool_registry.py
git commit -m "feat: 9 个工具的效果声明清单与启动期自检"
```

---

### Task 8: 工具处理器

**Files:**
- Create: `src/guardrail/tools/handlers.py`
- Test: `tests/test_tool_handlers.py`

**Interfaces:**
- Consumes: 商城的 HTTP 接口（Task 5）
- Produces:
  - `ToolContext(shop: httpx.AsyncClient)`
  - `async handle(tool: str, args: dict, ctx: ToolContext) -> dict`
  - `handle` 对写操作返回的派生字段：`update_price → absolute_delta_cents`；`create_order → order_id`；`create_coupon → coupon_id`；`refund_order → order_id`；`send_email → to_domain`

- [x] **Step 1: 写失败测试 `tests/test_tool_handlers.py`**

```python
import pytest

from guardrail.tools.handlers import ToolContext, handle
from shop.main import create_app
from tests.conftest import app_client


@pytest.fixture
async def ctx(tmp_path):
    async with app_client(create_app(str(tmp_path / "shop.db"))) as c:
        yield ToolContext(shop=c)


async def test_list_products(ctx):
    out = await handle("list_products", {}, ctx)
    assert len(out["products"]) >= 6


async def test_get_product(ctx):
    out = await handle("get_product", {"product_id": "p-iphone"}, ctx)
    assert out["product"]["id"] == "p-iphone"


async def test_update_price_returns_absolute_delta(ctx):
    out = await handle("update_price", {"product_id": "p-iphone", "delta_pct": -10.0}, ctx)
    assert out["before"]["list_price_cents"] == 599900
    assert out["absolute_delta_cents"] == out["after"]["list_price_cents"] - 599900


async def test_create_order_server_fills_unit_price_and_order_id(ctx):
    out = await handle("create_order", {"product_id": "p-tshirt-s", "qty": 2}, ctx)
    assert out["order"]["unit_price_cents"] > 0
    assert out["order_id"].startswith("o-")


async def test_create_coupon_returns_coupon_id(ctx):
    out = await handle(
        "create_coupon", {"code": "T20", "discount_pct": 20.0, "max_uses": 3}, ctx
    )
    assert out["coupon_id"].startswith("c-")


async def test_send_email_reports_domain(ctx):
    out = await handle("send_email", {"to": "a@external.example"}, ctx)
    assert out["to_domain"] == "external.example"


async def test_unknown_tool_raises(ctx):
    with pytest.raises(KeyError):
        await handle("nope", {}, ctx)
```

- [x] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_tool_handlers.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'guardrail.tools.handlers'`

- [x] **Step 3: 写 `src/guardrail/tools/handlers.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class ToolContext:
    shop: httpx.AsyncClient


async def _json(resp: httpx.Response) -> Any:
    resp.raise_for_status()
    return resp.json()


async def handle(tool: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """执行一个工具。只做转发与必要的服务端补全，不含任何策略判定。

    返回值额外携带投影所需的派生字段（如 absolute_delta_cents）：
    模型只报「降价 10%」，而效果声明需要「价格绝对值变化多少分」。
    """
    if tool == "list_products":
        params = {"category": args["category"]} if args.get("category") else None
        return {"products": await _json(await ctx.shop.get("/shop/v1/products", params=params))}

    if tool == "get_product":
        return {
            "product": await _json(await ctx.shop.get(f"/shop/v1/products/{args['product_id']}"))
        }

    if tool == "get_order":
        return {"order": await _json(await ctx.shop.get(f"/shop/v1/orders/{args['order_id']}"))}

    if tool == "update_price":
        before = await _json(await ctx.shop.get(f"/shop/v1/products/{args['product_id']}"))
        after = await _json(
            await ctx.shop.post(
                f"/shop/v1/products/{args['product_id']}/price",
                json={"delta_pct": args["delta_pct"]},
            )
        )
        return {
            "before": before,
            "after": after,
            "absolute_delta_cents": after["list_price_cents"] - before["list_price_cents"],
        }

    if tool == "update_stock":
        after = await _json(
            await ctx.shop.post(
                f"/shop/v1/products/{args['product_id']}/stock", json={"delta": args["delta"]}
            )
        )
        return {"after": after}

    if tool == "create_coupon":
        coupon = await _json(
            await ctx.shop.post(
                "/shop/v1/coupons",
                json={
                    "code": args["code"],
                    "discount_pct": args["discount_pct"],
                    "max_uses": args["max_uses"],
                },
            )
        )
        return {"coupon": coupon, "coupon_id": coupon["id"]}

    if tool == "create_order":
        order = await _json(
            await ctx.shop.post(
                "/shop/v1/orders",
                json={
                    "product_id": args["product_id"],
                    "qty": args["qty"],
                    "coupon_id": args.get("coupon_id"),
                },
            )
        )
        return {"order": order, "order_id": order["id"]}

    if tool == "refund_order":
        order = await _json(await ctx.shop.post(f"/shop/v1/orders/{args['order_id']}/refund"))
        return {"order": order, "order_id": order["id"]}

    if tool == "send_email":
        # 演示用：不外发，只回报目标域名，供污点规则判定。
        to = str(args.get("to", ""))
        return {"sent": False, "to_domain": to.split("@")[-1] if "@" in to else ""}

    raise KeyError(f"未知工具: {tool}")
```

> **评审修正（Fix B——工具执行缺参）**：`handle` 对缺失必需参数直接下标读取（`args['product_id']` 等）并抛 `KeyError`，这是有意为之——`handle` 只转发、不吞异常、不判策略。工具执行路径的畸形输入由 Task 9 的 `call_tool` 捕获 `KeyError` 并映射为 400（点名工具与缺失参数），商城自身 404/409 仍由 `httpx.HTTPStatusError` 透传。因此 `POST /v1/tools/get_product` 带 `args: {}` 返回 400 而非 500（回归用例 `tests/test_gateway_api.py::test_tool_call_missing_required_arg_returns_400`）。

- [x] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_tool_handlers.py -v`
Expected: 7 passed

- [x] **Step 5: Commit**

```bash
git add src/guardrail/tools/handlers.py tests/test_tool_handlers.py
git commit -m "feat: 工具处理器，含服务端派生字段补全"
```

---

### Task 9: 网关 API——会话与工具执行

**Files:**
- Create: `src/guardrail/protocols.py`
- Create: `src/guardrail/stores/__init__.py`（空）
- Create: `src/guardrail/stores/sqlite.py`
- Create: `src/guardrail/api/__init__.py`（空）
- Create: `src/guardrail/api/sessions.py`
- Create: `src/guardrail/api/tools.py`
- Create: `src/guardrail/preview_rules.py`
- Create: `src/guardrail/main.py`
- Test: `tests/test_gateway_api.py`

**Interfaces:**
- Consumes: `TOOL_SPECS`（Task 7）、`ToolContext`/`handle`（Task 8）、`build_shadow`/`project`/`compute_metrics`（Task 2、6）、`Settings`（Task 1）、`GET /shop/v1/coupons/{coupon_id}`（Task 5，预览加载计划引用的优惠券）
- Produces:
  - `guardrail.protocols.SessionRecord(session_id, agent_id, task_id, created_at)`
  - `guardrail.protocols.SessionStore`（Protocol）：`async load(session_id) -> SessionRecord | None`、`async save(record) -> None`
  - `guardrail.stores.sqlite.SqliteSessionStore(gateway_db_path)`
  - `guardrail.preview_rules.assert_action_applicable(tool, args, shadow) -> None`（预览前校验商城领域不变量，违反则抛 `ProjectionError`）
  - `guardrail.main.create_app(settings: Settings | None = None) -> FastAPI`
  - 路由 `POST /v1/sessions`、`POST /v1/tools/{name}`、`POST /v1/plans/preview`

- [x] **Step 1: 写失败测试 `tests/test_gateway_api.py`**

```python
import httpx
import pytest

from guardrail.config import Settings
from guardrail.main import create_app


@pytest.fixture
async def client(tmp_path):
    settings = Settings(
        shop_base_url="http://testserver",
        gateway_db_path=str(tmp_path / "gateway.db"),
    )
    app = create_app(settings)
    # 网关通过 httpx 调商城。测试里把两边的 app 绑在同一个 ASGI transport 上：
    # 商城的 create_app 与网关指向同一个 SQLite 文件，因此数据一致。
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_create_session(client):
    r = await client.post("/v1/sessions", json={"agent_id": "pricing_agent", "task_id": "t-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["agent_id"] == "pricing_agent"
    assert body["session_id"].startswith("s-")


async def test_tool_call_requires_valid_session(client):
    r = await client.post("/v1/tools/list_products", json={"session_id": "s-nope", "args": {}})
    assert r.status_code == 404


async def test_unknown_tool_returns_404(client):
    s = (await client.post("/v1/sessions", json={"agent_id": "a", "task_id": None})).json()
    r = await client.post("/v1/tools/nope", json={"session_id": s["session_id"], "args": {}})
    assert r.status_code == 404
```

- [x] **Step 2: 把测试替换为最终版**

Step 1 那版还覆盖不了端到端：网关要通过 httpx 回调商城，而 ASGI 内存测试里没有真实的商城进程在跑。解决办法是让 `create_app` 接受一个可选的 `shop_transport`（Step 8 实现），把商城 app 直接挂进内存——测试因此不需要起第二个进程。

把 `tests/test_gateway_api.py` 整体替换为：

```python
import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.config import Settings
from guardrail.main import create_app
from shop.main import create_app as create_shop_app
from tests.conftest import app_client


@pytest.fixture
async def client(tmp_path):
    # 两个 app 都需要各自的 lifespan：商城要建表/播种，网关要建会话库与 httpx 客户端。
    shop_app = create_shop_app(str(tmp_path / "shop.db"))
    async with LifespanManager(shop_app):
        settings = Settings(
            shop_base_url="http://shop.test",
            gateway_db_path=str(tmp_path / "gateway.db"),
        )
        gateway_app = create_app(settings, shop_transport=httpx.ASGITransport(app=shop_app))
        async with app_client(gateway_app) as c:
            yield c


async def _new_session(client) -> str:
    r = await client.post("/v1/sessions", json={"agent_id": "pricing_agent", "task_id": "t-1"})
    return r.json()["session_id"]


async def test_create_session(client):
    r = await client.post("/v1/sessions", json={"agent_id": "pricing_agent", "task_id": "t-1"})
    assert r.status_code == 200
    assert r.json()["session_id"].startswith("s-")


async def test_tool_call_requires_valid_session(client):
    r = await client.post("/v1/tools/list_products", json={"session_id": "s-nope", "args": {}})
    assert r.status_code == 404


async def test_unknown_tool_returns_404(client):
    sid = await _new_session(client)
    r = await client.post("/v1/tools/nope", json={"session_id": sid, "args": {}})
    assert r.status_code == 404


async def test_tool_call_end_to_end(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/tools/update_price",
        json={"session_id": sid, "args": {"product_id": "p-iphone", "delta_pct": -10.0}},
    )
    assert r.status_code == 200
    assert r.json()["result"]["after"]["list_price_cents"] == round(599900 * 0.9)


async def test_plan_preview_projects_final_state(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "update_price", "args": {"product_id": "p-iphone", "delta_pct": -5.0}},
                {"tool": "update_price", "args": {"product_id": "p-iphone", "delta_pct": -5.0}},
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["metrics"]["affected_product_count"] == 1
    expected = round(round(599900 * 0.95) * 0.95)
    assert body["entities"]["p-iphone"]["list_price_cents"] == expected


async def test_plan_preview_does_not_touch_real_shop(client):
    sid = await _new_session(client)
    await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "update_price", "args": {"product_id": "p-iphone", "delta_pct": -50.0}}
            ],
        },
    )
    r = await client.post(
        "/v1/tools/get_product",
        json={"session_id": sid, "args": {"product_id": "p-iphone"}},
    )
    assert r.json()["result"]["product"]["list_price_cents"] == 599900


async def test_plan_preview_allows_intra_plan_coupon(client):
    # create_coupon → create_order(带券)：计划内引用占位 id 不得被误判为「优惠券不存在」。
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {
                    "tool": "create_coupon",
                    "args": {"code": "S20", "discount_pct": 20.0, "max_uses": 5},
                },
                {
                    "tool": "create_order",
                    "args": {
                        "product_id": "p-tshirt-s",
                        "qty": 2,
                        "coupon_id": "preview-coupon-0",
                    },
                },
            ],
        },
    )
    assert r.status_code == 200
    # p-tshirt-s 标价 9900，八折 → round(9900 * 0.8) = 7920；现金影响 = 7920 * 2。
    assert r.json()["metrics"]["cash_impact_cents"] == 7920 * 2


async def test_plan_preview_allows_intra_plan_refund(client):
    # create_order → refund_order：计划内引用占位 id 不得被误判为「订单不存在」。
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "create_order", "args": {"product_id": "p-tshirt-s", "qty": 1}},
                {"tool": "refund_order", "args": {"order_id": "preview-order-0"}},
            ],
        },
    )
    assert r.status_code == 200


async def test_plan_preview_rejects_non_numeric_delta_pct(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "update_price", "args": {"product_id": "p-iphone", "delta_pct": "abc"}}
            ],
        },
    )
    assert r.status_code == 400
    assert "delta_pct" in r.json()["detail"]


async def test_plan_preview_rejects_non_numeric_qty(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {"tool": "create_order", "args": {"product_id": "p-tshirt-s", "qty": "abc"}}
            ],
        },
    )
    assert r.status_code == 400
    assert "qty" in r.json()["detail"]


async def test_plan_preview_rejects_missing_required_arg(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [{"tool": "update_price", "args": {"product_id": "p-iphone"}}],
        },
    )
    assert r.status_code == 400
    assert "delta_pct" in r.json()["detail"]


@pytest.mark.parametrize("qty", ["0", -1.5, 0.0, True, -3])
async def test_plan_preview_rejects_non_positive_qty(client, qty):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [{"tool": "create_order", "args": {"product_id": "p-tshirt-s", "qty": qty}}],
        },
    )
    assert r.status_code == 400


@pytest.mark.parametrize("coupon_id", [0, False, [], {}])
async def test_plan_preview_rejects_malformed_coupon_id(client, coupon_id):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {
                    "tool": "create_order",
                    "args": {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": coupon_id},
                }
            ],
        },
    )
    assert r.status_code == 400
    assert "coupon_id" in r.json()["detail"]


async def test_plan_preview_allows_absent_coupon_id(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [{"tool": "create_order", "args": {"product_id": "p-tshirt-s", "qty": 1}}],
        },
    )
    assert r.status_code == 200


async def test_plan_preview_allows_null_coupon_id(client):
    sid = await _new_session(client)
    r = await client.post(
        "/v1/plans/preview",
        json={
            "session_id": sid,
            "actions": [
                {
                    "tool": "create_order",
                    "args": {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": None},
                }
            ],
        },
    )
    assert r.status_code == 200
```

- [x] **Step 3: 写 `src/guardrail/protocols.py`**

```python
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class SessionRecord(BaseModel):
    session_id: str
    agent_id: str
    task_id: str | None = None
    created_at: str


class SessionStore(Protocol):
    """会话存储协议。

    当前唯一实现是 SQLite（单进程）。多进程扩展只需新增一个实现——
    调用方代码不变。见 spec §19.1 / §19.2。
    """

    async def load(self, session_id: str) -> SessionRecord | None: ...

    async def save(self, record: SessionRecord) -> None: ...
```

- [x] **Step 4: 写 `src/guardrail/stores/sqlite.py`**

```python
from __future__ import annotations

from pathlib import Path

import aiosqlite

from guardrail.protocols import SessionRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  task_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_task ON sessions(task_id);
"""


class SqliteSessionStore:
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
            raise RuntimeError("SqliteSessionStore.connect() 未调用")
        return self._conn

    async def load(self, session_id: str) -> SessionRecord | None:
        async with self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return SessionRecord(
            session_id=row["id"],
            agent_id=row["agent_id"],
            task_id=row["task_id"],
            created_at=row["created_at"],
        )

    async def save(self, record: SessionRecord) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO sessions (id, agent_id, task_id, created_at)"
            " VALUES (?, ?, ?, ?)",
            (record.session_id, record.agent_id, record.task_id, record.created_at),
        )
        await self.conn.commit()
```

- [x] **Step 5: 写 `src/guardrail/api/sessions.py`**

```python
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

from guardrail.protocols import SessionRecord

router = APIRouter(prefix="/v1", tags=["sessions"])


class SessionCreate(BaseModel):
    agent_id: str
    task_id: str | None = None


@router.post("/sessions")
async def create_session(request: Request, body: SessionCreate) -> dict[str, str | None]:
    record = SessionRecord(
        session_id=f"s-{uuid.uuid4().hex[:12]}",
        agent_id=body.agent_id,
        task_id=body.task_id,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    await request.app.state.sessions.save(record)
    return record.model_dump()
```

- [x] **Step 6: 写 `src/guardrail/api/tools.py`**

```python
from __future__ import annotations

import math
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from guardrail.models import ProjectedState, ShadowState, compute_metrics
from guardrail.preview_rules import assert_action_applicable
from guardrail.projection import ProjectionError, build_shadow, project
from guardrail.tools.handlers import ToolContext, handle
from guardrail.tools.registry import TOOL_SPECS

router = APIRouter(prefix="/v1", tags=["tools"])


class ToolCall(BaseModel):
    session_id: str
    args: dict[str, Any] = {}


class PlannedAction(BaseModel):
    tool: str
    args: dict[str, Any] = {}


class PlanPreview(BaseModel):
    session_id: str
    actions: list[PlannedAction]


async def _require_session(request: Request, session_id: str) -> None:
    if await request.app.state.sessions.load(session_id) is None:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")


@router.post("/tools/{name}")
async def call_tool(request: Request, name: str, body: ToolCall) -> dict[str, Any]:
    await _require_session(request, body.session_id)
    if name not in TOOL_SPECS:
        raise HTTPException(status_code=404, detail=f"未知工具: {name}")
    ctx = ToolContext(shop=request.app.state.shop)
    try:
        result = await handle(name, body.args, ctx)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text) from exc
    except KeyError as exc:
        # handle 直接下标读取必需参数，缺参抛 KeyError。畸形输入必须是 400，
        # 而不是让 KeyError 逃逸成 500（商城自身的 404/409 仍由 HTTPStatusError 透传）。
        raise HTTPException(
            status_code=400, detail=f"工具 {name!r} 缺少必需参数 {exc.args[0]!r}"
        ) from exc
    return {"tool": name, "result": result}


@router.post("/plans/preview")
async def preview_plan(request: Request, body: PlanPreview) -> ProjectedState:
    """在影子状态上投影整个计划，返回终态与业务指标。不触碰真实商城。

    逐步投影，每步的派生参数以**当前影子状态**为基准——同一商品在计划里被
    改价多次时，第二次的基准必须是第一次投影后的价格，否则投影与真实执行的
    终态会对不上（Task 10 的属性测试专门验证这一点）。
    """
    await _require_session(request, body.session_id)
    shop = request.app.state.shop

    products = (await shop.get("/shop/v1/products")).json()
    coupons, orders = await _load_referenced_entities(shop, body.actions)
    before = build_shadow(products=products, coupons=coupons, orders=orders)

    shadow = before.clone()
    for step, action in enumerate(body.actions):
        if action.tool not in TOOL_SPECS:
            raise HTTPException(status_code=400, detail=f"未知工具: {action.tool}")
        try:
            args = derive_projection_args(action, shadow, step)
            assert_action_applicable(action.tool, args, shadow)
            shadow = project(shadow, TOOL_SPECS, [(action.tool, args)])
        except ProjectionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    entities = {k: v for k, v in shadow.products.items() if f"product:{k}" in shadow.touched}
    return ProjectedState(
        entities=entities,
        metrics=compute_metrics(before, shadow),
        triggered_rules=[],
    )


_PREVIEW_ID_PREFIX = "preview-"


def _is_preview_id(value: object) -> bool:
    """是否为计划内部合成的占位 id（`preview-coupon-{step}` / `preview-order-{step}`）。

    真实商城的 id 有各自前缀（coupon 为 `c-…`、order 为 `o-…`），`preview-` 前缀
    只用于投影时给计划内新建实体占位，不可能与真实 id 撞车。`_load_referenced_entities`
    据此把「计划内引用」与「引用商城已有实体」区分开。
    """
    return str(value).startswith(_PREVIEW_ID_PREFIX)


async def _load_referenced_entities(
    shop: httpx.AsyncClient, actions: list[PlannedAction]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把计划里所有动作 args 引用的 coupon_id / order_id 装进影子状态。

    计划引用了一个商城里真实存在的实体时，影子必须装下它，否则用券下单会被
    投影成不打折、退款会解析不到目标。引用的实体在商城里不存在时，直接 400——
    那是一份引用了不存在实体的计划，绝不能让审批人看到一份「看起来没问题」的
    投影终态。

    计划内部新建实体的占位 id（`preview-` 前缀）不在此列：它们由
    `derive_projection_args` 在逐步投影时合成，本函数运行时尚不存在于商城，
    去查只会误报 400。
    """
    coupon_ids: set[str] = set()
    order_ids: set[str] = set()
    for action in actions:
        coupon_id = action.args.get("coupon_id")
        if isinstance(coupon_id, str) and coupon_id and not _is_preview_id(coupon_id):
            coupon_ids.add(coupon_id)
        order_id = action.args.get("order_id")
        if order_id and not _is_preview_id(order_id):
            order_ids.add(str(order_id))

    coupons: list[dict[str, Any]] = []
    for coupon_id in sorted(coupon_ids):
        resp = await shop.get(f"/shop/v1/coupons/{coupon_id}")
        if resp.status_code == 404:
            raise HTTPException(status_code=400, detail=f"计划引用的优惠券不存在: {coupon_id}")
        resp.raise_for_status()
        coupons.append(resp.json())

    orders: list[dict[str, Any]] = []
    for order_id in sorted(order_ids):
        resp = await shop.get(f"/shop/v1/orders/{order_id}")
        if resp.status_code == 404:
            raise HTTPException(status_code=400, detail=f"计划引用的订单不存在: {order_id}")
        resp.raise_for_status()
        orders.append(resp.json())

    return coupons, orders


def derive_projection_args(
    action: PlannedAction, shadow: ShadowState, step: int
) -> dict[str, Any]:
    """把模型报的意图参数换算成效果声明需要的派生字段。

    模型只会说「降价 5%」，而效果声明需要「价格变化多少分」。
    换算必须在网关侧做，因为它依赖当前价格；基准是**投影后的影子状态**。
    """
    args = dict(action.args)
    if action.tool == "update_price":
        product = shadow.products.get(str(args.get("product_id")))
        if product is not None:
            # 与商城 update_price 逐字对齐：商城存 round(price * (1 + d/100))，
            # 所以绝对变化量必须是 round(price * (1 + d/100)) - price。
            # 输入有限不代表中间量有限：1e305 会让中间量溢出为 inf，round(inf) 抛
            # OverflowError（非 ProjectionError）逃逸成 500；守卫中间量本身。
            delta_pct = float(args["delta_pct"])
            target_cents = product.list_price_cents * (1 + delta_pct / 100)
            if not math.isfinite(target_cents):
                raise ProjectionError(
                    f"投影失败：动作 {action.tool!r} 的参数 'delta_pct' "
                    f"使价格计算溢出为非有限值，收到 {delta_pct!r}"
                )
            args["absolute_delta_cents"] = round(target_cents) - product.list_price_cents
    return args
```

- [x] **Step 7: 补全 `derive_projection_args`，覆盖全部写工具**

Step 6 的版本只处理 `update_price`。**其余四个写工具的效果声明引用的字段（`coupon_id` / `order_id` / `unit_price_cents`）在模型传来的 args 里根本不存在**——它们是商城在真实执行时才生成的。计划预览必须在网关侧合成它们，否则这些工具的投影结果是个空壳，审批人看到的终态就是假的。

先写测试 `tests/test_projection_args.py`：

```python
from guardrail.api.tools import PlannedAction, derive_projection_args
from guardrail.projection import build_shadow

SHADOW = build_shadow(
    products=[
        {
            "id": "p-tshirt-s",
            "name": "T",
            "category": "夏季款",
            "cost_price_cents": 3500,
            "list_price_cents": 9900,
            "stock": 300,
        }
    ],
    coupons=[],
    orders=[],
)


def test_update_price_derives_absolute_delta_from_shadow_price():
    out = derive_projection_args(
        PlannedAction(tool="update_price", args={"product_id": "p-tshirt-s", "delta_pct": -10.0}),
        SHADOW,
        0,
    )
    assert out["absolute_delta_cents"] == -990


def test_update_price_未命中商品时不注入字段():
    out = derive_projection_args(
        PlannedAction(tool="update_price", args={"product_id": "p-nope", "delta_pct": -10.0}),
        SHADOW,
        0,
    )
    assert "absolute_delta_cents" not in out


def test_create_coupon_synthesizes_deterministic_coupon_id():
    out = derive_projection_args(
        PlannedAction(
            tool="create_coupon", args={"code": "S20", "discount_pct": 20.0, "max_uses": 5}
        ),
        SHADOW,
        2,
    )
    assert out["coupon_id"] == "preview-coupon-2"


def test_create_order_synthesizes_id_coupon_and_unit_price():
    out = derive_projection_args(
        PlannedAction(tool="create_order", args={"product_id": "p-tshirt-s", "qty": 3}),
        SHADOW,
        1,
    )
    assert out["order_id"] == "preview-order-1"
    assert out["coupon_id"] is None
    assert out["unit_price_cents"] == 9900


def test_create_order_unknown_coupon_leaves_price_undiscounted():
    out = derive_projection_args(
        PlannedAction(
            tool="create_order",
            args={"product_id": "p-tshirt-s", "qty": 1, "coupon_id": "c-not-in-shadow"},
        ),
        SHADOW,
        0,
    )
    assert out["unit_price_cents"] == 9900


def test_refund_order_keeps_real_order_id_from_args():
    out = derive_projection_args(
        PlannedAction(tool="refund_order", args={"order_id": "o-real"}), SHADOW, 0
    )
    assert out["order_id"] == "o-real"


def test_read_and_noop_tools_pass_args_through_unchanged():
    out = derive_projection_args(
        PlannedAction(tool="send_email", args={"to": "a@b.test"}), SHADOW, 0
    )
    assert out == {"to": "a@b.test"}
```

Run: `uv run pytest tests/test_projection_args.py -v`
Expected: FAIL —— 4 条失败（`create_coupon` / `create_order` 相关）

然后把 `derive_projection_args` 替换为完整版：

```python
def _require_float_arg(tool: str, args: dict[str, Any], key: str) -> float:
    """取出并校验一个浮点参数；缺失 / 非数值 / 布尔 / NaN·Inf 都抛 ProjectionError（映射 400）。"""
    if key not in args:
        raise ProjectionError(f"投影失败：动作 {tool!r} 缺少必需参数 {key!r}")
    value = args[key]
    if isinstance(value, bool):
        raise ProjectionError(
            f"投影失败：动作 {tool!r} 的参数 {key!r} 必须是浮点数，收到 {value!r}"
        )
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ProjectionError(
            f"投影失败：动作 {tool!r} 的参数 {key!r} 无法转换为浮点数，收到 {value!r}"
        ) from None
    if not math.isfinite(result):
        raise ProjectionError(
            f"投影失败：动作 {tool!r} 的参数 {key!r} 必须是有限浮点数，收到 {value!r}"
        )
    return result


def _require_int_arg(tool: str, args: dict[str, Any], key: str) -> int:
    """取出并校验一个整数参数；缺失 / 非整数（含非积分浮点）/ 布尔都抛 ProjectionError（映射 400）。

    与商城 Pydantic 的 `int` 字段对齐：积分浮点（-4.0）收敛为 int，非积分浮点
    （1.5）与 `bool` 拒绝——绝不能像裸 `int(1.5)` 那样截断成 1 去投影一份商城会
    拒绝的计划。
    """
    if key not in args:
        raise ProjectionError(f"投影失败：动作 {tool!r} 缺少必需参数 {key!r}")
    value = args[key]
    if isinstance(value, bool):
        raise ProjectionError(
            f"投影失败：动作 {tool!r} 的参数 {key!r} 必须是整数，收到 {value!r}"
        )
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise ProjectionError(
            f"投影失败：动作 {tool!r} 的参数 {key!r} 必须是整数，收到非整数值 {value!r}"
        )
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ProjectionError(
            f"投影失败：动作 {tool!r} 的参数 {key!r} 无法转换为整数，收到 {value!r}"
        ) from None


def _require_optional_str_arg(tool: str, args: dict[str, Any], key: str) -> str | None:
    """取出并校验一个可选字符串参数；缺失 / `None` 视为「未指名」。

    只有非空 `str` 合法。空串、`0`、`false`、`[]`、`{}` 等既非 `None` 也非
    非空字符串的值都是畸形输入，抛 `ProjectionError`（映射 400），而不是让
    下游的 Pydantic 快照校验把它变成 500。
    """
    if key not in args:
        return None
    value = args[key]
    if value is None:
        return None
    if isinstance(value, str) and value != "":
        return value
    raise ProjectionError(
        f"投影失败：动作 {tool!r} 的参数 {key!r} 必须是字符串，收到 {value!r}"
    )


def derive_projection_args(
    action: PlannedAction, shadow: ShadowState, step: int
) -> dict[str, Any]:
    """把模型报的意图参数换算成效果声明需要的派生字段。

    两类补全：

    1. 百分比 → 绝对值（update_price）：效果声明要「价格变化多少分」，
       而模型只说「降价 5%」。基准是**投影后的影子状态**，因为同一商品
       在一个计划里可能被改价多次，第二次的基准必须是第一次之后的价格。
    2. 服务端生成 ID（create_coupon / create_order）：真实执行时 ID 由商城
       生成，预览时用确定性的占位 ID（`preview-coupon-{step}`）。
       占位 ID 只用于聚合指标（毛利、受影响商品数），不影响审批人看到的终态。

    参数本身也必须合法：`delta_pct` / `qty` 缺失或非数值时抛 `ProjectionError`
    （由 `preview_plan` 统一映射为 400），而不是让 `float()` / `int()` 裸抛
    `ValueError` / `KeyError` 变成 500。
    """
    args = dict(action.args)

    if action.tool == "update_price":
        product = shadow.products.get(str(args.get("product_id")))
        if product is not None:
            # 与商城 update_price 的算术逐字对齐：商城存 round(price * (1 + d/100))，
            # 所以绝对变化量必须是 round(price * (1 + d/100)) - price。若改用
            # round(price * d / 100)，当中间值恰好落在 .5 边界时会差一分（
            # before=9、d=50 时商城存 14、旧公式得 13）。
            delta_pct = _require_float_arg(action.tool, args, "delta_pct")
            # 输入有限不代表中间量有限：1e305 能让 price * (1 + d/100) 溢出为 inf，
            # round(inf) 抛 OverflowError（不是 ProjectionError）会逃逸成 500。
            # 守卫必须落在进入 round() 的那个值上，而不只是解析后的输入。
            target_cents = product.list_price_cents * (1 + delta_pct / 100)
            if not math.isfinite(target_cents):
                raise ProjectionError(
                    f"投影失败：动作 {action.tool!r} 的参数 'delta_pct' "
                    f"使价格计算溢出为非有限值，收到 {delta_pct!r}"
                )
            args["absolute_delta_cents"] = round(target_cents) - product.list_price_cents

    elif action.tool == "create_coupon":
        args["coupon_id"] = f"preview-coupon-{step}"

    elif action.tool == "create_order":
        args["order_id"] = f"preview-order-{step}"
        qty = _require_int_arg(action.tool, args, "qty")
        args["qty"] = qty
        product = shadow.products.get(str(args.get("product_id")))
        unit_price = product.list_price_cents if product is not None else 0
        coupon_id = _require_optional_str_arg(action.tool, args, "coupon_id")
        args["coupon_id"] = coupon_id
        if coupon_id is not None:
            coupon = shadow.coupons.get(coupon_id)
            if coupon is not None:
                unit_price = round(unit_price * (1 - coupon.discount_pct / 100))
        args["unit_price_cents"] = unit_price

    return args
```

Run: `uv run pytest tests/test_projection_args.py -v`
Expected: 7 passed

- [x] **Step 8: 写 `src/guardrail/main.py`**

```python
from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from guardrail.api import sessions, tools
from guardrail.config import Settings, get_settings
from guardrail.stores.sqlite import SqliteSessionStore
from guardrail.tools.registry import assert_specs_valid


def create_app(
    settings: Settings | None = None,
    shop_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """shop_transport 仅用于测试：把商城 app 挂在内存里，免起真实进程。"""
    resolved = settings or get_settings()
    assert_specs_valid()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = SqliteSessionStore(resolved.gateway_db_path)
        await store.connect()
        app.state.sessions = store
        app.state.shop = httpx.AsyncClient(
            base_url=resolved.shop_base_url, transport=shop_transport
        )
        yield
        await app.state.shop.aclose()
        await store.close()

    app = FastAPI(title="会话级风险护栏", lifespan=lifespan)
    app.include_router(sessions.router)
    app.include_router(tools.router)
    return app


app = create_app()
```

- [x] **Step 9: 跑测试确认通过**

Run: `uv run pytest tests/test_gateway_api.py -v`
Expected: 28 passed（含三轮评审修正新增的引用实体加载、计划内引用、拒绝与畸形参数用例）

若 `test_plan_preview_projects_final_state` 的期望值不对，用真实计算结果修正断言——**不要改实现去迁就断言**。

- [x] **Step 10: Commit**

```bash
git add src/guardrail/protocols.py src/guardrail/stores/ src/guardrail/api/ src/guardrail/main.py tests/test_gateway_api.py
git commit -m "feat: 网关 API——会话、工具执行与计划投影预览"
```

> **评审修正（review-required fixes）**：上述逐字实现有三处投影偏离真实执行的缺陷，已在本任务内修正，后续任务生成的代码须以修正版为准：
> 1. `derive_projection_args` 的 `update_price` 派生量改为 `round(price * (1 + d/100)) - price`（与商城逐字对齐，避免 `.5` 边界差一分）；回归用例见 `tests/test_projection_args.py::test_update_price_delta_matches_shop_rounding`。
> 2. `preview_plan` 不再用 `coupons=[]` / `orders=[]` 构建影子，而是通过 `_load_referenced_entities` 把计划 args 引用的 `coupon_id` / `order_id` 装入影子；引用的实体不存在时 400。依赖商城新路由 `GET /shop/v1/coupons/{coupon_id}`（见 Task 5）。
> 3. 新增 `src/guardrail/preview_rules.py::assert_action_applicable`，在逐步投影**应用前**校验商城领域不变量（改价后价格 > 0、库存 >= 0、`0 < discount_pct < 100`、下单数量为正/商品与优惠券存在且未用尽、退款订单存在且为 `created`），违反则抛 `ProjectionError`。与商城的逐条一致性由 `tests/test_preview_rules.py` 的 parity 测试保证。

> **第二轮评审修正（review-required fixes）**：第一轮修正的实体存在性检查误伤了「计划内先建后用」的序列——`derive_projection_args` 合成 `preview-` 前缀占位 id，`_load_referenced_entities` 却把它当真实引用去商城查，导致 `create_coupon → create_order(带券)` 与 `create_order → refund_order` 在循环前就被 400。同时 `derive_projection_args` 对畸形参数裸调 `float()`/`int()`，落在 `try/except ProjectionError` 之外会 500。修正如下：
> 4. `_load_referenced_entities` 跳过 `preview-` 前缀的占位 id（新增模块常量 `_PREVIEW_ID_PREFIX` 与 `_is_preview_id`），只对真实 `c-…`/`o-…` 去商城查存在性；回归用例 `tests/test_gateway_api.py::test_plan_preview_allows_intra_plan_coupon` / `test_plan_preview_allows_intra_plan_refund`。
> 5. `derive_projection_args` 新增 `_require_float_arg` / `_require_int_arg`，`delta_pct`/`qty` 缺失或非数值时抛 `ProjectionError`；`preview_plan` 的 `try/except ProjectionError` 上移覆盖 `derive_projection_args`，畸形参数返回 400 并点名动作与字段（不再 500）。回归用例 `test_plan_preview_rejects_non_numeric_delta_pct` / `test_plan_preview_rejects_non_numeric_qty` / `test_plan_preview_rejects_missing_required_arg`。

> **第三轮评审修正（review-required fixes）**：第二轮修正仍有两处「预览会放行商城拒绝的计划 / 畸形输入会 500」的缺陷：
> 6. `qty` 的合法化只用于派生 `negative_qty`，`args["qty"]` 仍是调用方原始值，而 `preview_rules._check_create_order` 的正数校验 gate 在 `isinstance(qty, int)` 上，于是 `"0"`/`-1.5`/`0.0` 等非 `int` 值直接漏判，投影出商城会在执行时拒绝的 qty 0/-1 订单。**选择的方案**：在 `derive_projection_args` 里把 `args["qty"]` 重写为 `_require_int_arg` 收敛出的 int（与 `negative_qty` 同源），正数校验即可看到已验证值；`"0"`/`-1.5`/`0.0`/`True`/`-3` 均 400。回归用例 `test_plan_preview_rejects_non_positive_qty`。
> 7. `coupon_id` 所有守卫都是 truthiness 判断，falsy 非字符串（`0`/`false`/`[]`/`{}`）穿透到 `projection.py` 的 `OrderSnapshot(coupon_id=...)`，触发 Pydantic `ValidationError`（非 `ProjectionError`）→ 500。新增 `_require_optional_str_arg`：`coupon_id` 只能是缺失/`None`（视为「无券」）或非空 `str`，其余一律 400 并点名 `coupon_id`；`_load_referenced_entities` 与 `_check_create_order` 的守卫分别改为字符串判定 / `is not None`，与之一致。回归用例 `test_plan_preview_rejects_malformed_coupon_id` / `test_plan_preview_allows_absent_coupon_id` / `test_plan_preview_allows_null_coupon_id`。

> **第四轮评审修正（review-required fixes）**：前三轮各修一个字段，根因是结构性的——网关侧强转太宽松（`int()`/`float()`/`str()` 几乎什么都收），而商城 Pydantic 字段严格，于是每处宽松强转都是一处「预览会放行商城会拒绝的计划」。本轮把整条「请求体 → 影子状态」路径上的强转统一收口为至少与商城 Pydantic 同样严格：
> 8. 整数（`projection._coerce_int` / `tools._require_int_arg`）：非积分浮点（`qty: 1.5` / `max_uses: 1.5`）拒绝而非 `int()` 截断；`bool` 仍拒绝。`_coerce_int` 委托给 `_coerce_integral`（`is_integer()` 检查），`_require_int_arg` 施加同样检查。回归用例 `test_plan_preview_rejects_non_integral_qty` / `test_plan_preview_rejects_non_integral_max_uses`。
> 9. 字符串（`projection._coerce_str`）：非 `str`（`code: null`/`0`/`{}`）拒绝，不再 `str(None)` 成 `"None"` 放行——商城 `CouponCreate.code: str` 会拒绝它们。回归用例 `test_plan_preview_rejects_non_string_code`。
> 10. 浮点（`tools._require_float_arg` / `projection._coerce_float`）：`NaN`/`±Infinity` 拒绝。`float("nan")`/`float("inf")`/`float("Infinity")` 都能解析成功，必须查结果值的有限性而非字符串；此前 `delta_pct: "nan"` 冲到 `round(...)` 抛 `ValueError` 逃出 `except ProjectionError` 变成 500。回归用例 `test_plan_preview_rejects_non_finite_delta_pct`。
> 11. 工具执行路径缺参（Fix B）：`call_tool` 只捕获 `httpx.HTTPStatusError`，`handle` 直接下标读必需参数，`args: {}` 会 `KeyError` → 500。新增 `except KeyError` 映射为 400（点名工具与缺失参数）；商城自身 404/409 仍由 `HTTPStatusError` 透传。回归用例 `test_tool_call_missing_required_arg_returns_400`。
> 12. 一致性测试 `tests/test_preview_rules.py::test_preview_rules_parity` 的 `_gateway_ok` 补上 `project(...)` 步骤（此前只跑 `derive_projection_args` + `assert_action_applicable`，覆盖不到 `projection.py` 里的类型强转），并新增整数/字符串严格用例。`delta_pct` 的 `nan`/`inf` 不进 parity——商城自身会 `round(nan)` 抛异常崩溃（内存测试里 `httpx.ASGITransport` 会把它重抛出来），无干净的 reject 可对齐，网关侧 400 由回归用例保证。

> **第五轮评审修正（review-required fixes）**：第四轮的有限性检查只作用于**解析后的输入**，而 `round()` 收到的是**中间计算量**：
> 13. `delta_pct` 输入有限（如 `1e305`）仍可能让 `price * (1 + d/100)` 溢出为 `inf`，`round(inf)` 抛 `OverflowError`（非 `ProjectionError`）逃逸出 `preview_plan` 的 `except ProjectionError` 变成 500。在 `derive_projection_args`（`src/guardrail/api/tools.py`）与 `_check_update_price`（`src/guardrail/preview_rules.py`）两处，把中间量先算入局部变量、`math.isfinite(...)` 校验通过后再 `round()`；不有限则抛 `ProjectionError` 点名动作与字段（`preview_rules.py` 需 `import math`）。保留 `_require_float_arg` 的输入有限性检查，此为**追加**守卫而非替换。回归用例 `test_plan_preview_rejects_overflowing_delta_pct`（端到端 `delta_pct: 1e305` 返回 400 而非 500）。

---

### Task 10: 投影一致性属性测试（spec §12.2 验收）

这是本计划的**验收关卡**。投影错了，后面所有审批都在骗人。

> 网关侧的商城规则一致性由 `tests/test_preview_rules.py::test_preview_rules_parity` 保证（同一组用例同时驱动 `assert_action_applicable` 与真实商城，断言 accept/reject 一致）——它已在 Task 9 的评审修正中落地，本任务不必重复。

**Files:**
- Create: `tests/test_projection_consistency.py`

**Interfaces:**
- Consumes: 全部前置任务
- Produces: 无（纯验证）

- [x] **Step 1: 写属性测试**

```python
import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from guardrail.api.tools import PlannedAction, derive_projection_args
from guardrail.preview_rules import assert_action_applicable
from guardrail.projection import ProjectionError, build_shadow, project
from guardrail.tools.handlers import ToolContext, handle
from guardrail.tools.registry import TOOL_SPECS
from shop.main import create_app
from shop.store import ShopStore
from tests.conftest import app_client

_product_id = st.sampled_from(["p-iphone", "p-ipad", "p-airpods"])
_delta_pct = st.integers(min_value=-50, max_value=50)

# 奇数分商品：标价 9 分。update_price 的两种算术只在「当前价为奇数分、且 round 的
# 中间值恰好落在 .5」时差一分；现有演示商品标价全是偶数分，随机采样几乎撞不到边界，
# 故额外种一个奇数分商品，并在每个 example 末尾强制追加一个 .5 边界步骤。
_ODD_PRODUCT_ID = "p-odd-cents"


@st.composite
def _price_changes(draw):
    """随机改价序列，末尾固定一个「奇数分商品 +50%」的 .5 边界步骤。

    随机步骤覆盖「同一商品多次改价、第二次以投影后价格为基准」的一般情况；
    边界步骤触发 round(9 * 1.5) - 9 = 5（真实）vs 旧公式 round(9 * 50 / 100) = 4
    的一分之差，确保验收关卡真的 gate 到网关真实派生路径。
    """
    n = draw(st.integers(min_value=0, max_value=5))
    steps = [(draw(_product_id), draw(_delta_pct)) for _ in range(n)]
    steps.append((_ODD_PRODUCT_ID, 50))
    return steps


@hsettings(max_examples=40, deadline=None)
@given(changes=_price_changes())
def test_projection_matches_real_execution(changes):
    """同一组动作，先投影、后真实执行，终态必须一致。

    投影错了，计划审批就是在骗人——这条测试是本计划的验收关卡。

    两点实现约束：
    1. hypothesis 的 @given **不支持 async 测试函数**，所以这里写成同步函数、
       用 asyncio.run 驱动异步逻辑。
    2. 每个 example 必须用**独立的临时数据库**，否则状态会在 example 之间累积，
       价格越跑越偏，测试会以看似随机的方式失败。
    """
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_assert_projection_matches(str(Path(tmp) / "shop.db"), changes))


async def _seed_odd_priced_product(db_path: str) -> None:
    """往测试商城的临时库里额外种一个奇数分商品（必须在 create_app 之前种）。"""
    store = ShopStore(db_path)
    await store.connect()
    await store.init_schema()
    await store.conn.execute(
        "INSERT OR REPLACE INTO products"
        " (id, name, category, cost_price_cents, list_price_cents, stock)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (_ODD_PRODUCT_ID, "奇数分测试品", "测试", 5, 9, 10),
    )
    await store.conn.commit()
    await store.close()


async def _assert_projection_matches(db_path: str, changes: list[tuple[str, int]]) -> None:
    await _seed_odd_priced_product(db_path)
    async with app_client(create_app(db_path)) as client:
        ctx = ToolContext(shop=client)
        products = (await client.get("/shop/v1/products")).json()
        before = build_shadow(products=products, coupons=[], orders=[])

        # 1) 逐步投影：走网关真实的派生 + 投影路径（与 POST /v1/plans/preview 一致）
        shadow = before.clone()
        for step, (pid, delta) in enumerate(changes):
            action = PlannedAction(
                tool="update_price", args={"product_id": pid, "delta_pct": delta}
            )
            args = derive_projection_args(action, shadow, step)
            shadow = project(shadow, TOOL_SPECS, [("update_price", args)])

        # 2) 真实执行同一组 delta
        for pid, delta in changes:
            await handle("update_price", {"product_id": pid, "delta_pct": delta}, ctx)

        # 3) 比对终态
        for pid, _ in changes:
            real = (await client.get(f"/shop/v1/products/{pid}")).json()
            assert shadow.products[pid].list_price_cents == real["list_price_cents"], (
                f"{pid} 投影 {shadow.products[pid].list_price_cents} "
                f"≠ 真实 {real['list_price_cents']}；delta 序列 {changes}"
            )


async def test_projection_does_not_mutate_source_state(tmp_path):
    async with app_client(create_app(str(tmp_path / "shop.db"))) as client:
        products = (await client.get("/shop/v1/products")).json()
        before = build_shadow(products=products, coupons=[], orders=[])
        original = before.products["p-iphone"].list_price_cents
        project(
            before,
            TOOL_SPECS,
            [("update_price", {"product_id": "p-iphone", "absolute_delta_cents": -10000})],
        )
        assert before.products["p-iphone"].list_price_cents == original


# 下单属性测试的商品池（都是 demo 数据里存在的商品）。
_ORDER_PRODUCT_ID = st.sampled_from(["p-tshirt-s", "p-shorts", "p-cap", "p-sandals"])


@st.composite
def _orders(draw):
    """随机下单序列：每单随机选商品与数量，随机决定是否带券。"""
    n = draw(st.integers(min_value=1, max_value=6))
    steps = []
    for _ in range(n):
        steps.append(
            (
                draw(_ORDER_PRODUCT_ID),
                draw(st.integers(min_value=1, max_value=5)),
                draw(st.booleans()),
            )
        )
    return steps


@hsettings(max_examples=40, deadline=None)
@given(orders=_orders())
def test_order_projection_matches_real_execution(orders):
    """同一组下单动作，先投影、后真实执行，终态必须一致（覆盖 create_order）。"""
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_assert_order_projection_matches(str(Path(tmp) / "shop.db"), orders))


async def _assert_order_projection_matches(db_path: str, orders) -> None:
    async with app_client(create_app(db_path)) as client:
        ctx = ToolContext(shop=client)
        coupon = await handle(
            "create_coupon", {"code": "PCT", "discount_pct": 20.0, "max_uses": 1000}, ctx
        )
        coupon_id = coupon["coupon_id"]
        products = (await client.get("/shop/v1/products")).json()
        coupon_row = (await client.get(f"/shop/v1/coupons/{coupon_id}")).json()
        before = build_shadow(products=products, coupons=[coupon_row], orders=[])
        initial_stock = {p["id"]: p["stock"] for p in products}

        shadow = before.clone()
        projected_total = 0
        real_total = 0
        used_orders = 0
        for step, (pid, qty, use_coupon) in enumerate(orders):
            args = {
                "product_id": pid,
                "qty": qty,
                "coupon_id": coupon_id if use_coupon else None,
            }
            action = PlannedAction(tool="create_order", args=args)
            derived = derive_projection_args(action, shadow, step)
            assert_action_applicable("create_order", derived, shadow)
            shadow = project(shadow, TOOL_SPECS, [("create_order", derived)])
            projected_total += shadow.orders[f"preview-order-{step}"].unit_price_cents * qty

            out = await handle(
                "create_order",
                {"product_id": pid, "qty": qty, "coupon_id": coupon_id if use_coupon else None},
                ctx,
            )
            real_total += out["order"]["unit_price_cents"] * qty
            used_orders += 1 if use_coupon else 0

        for pid in {p[0] for p in orders}:
            real = (await client.get(f"/shop/v1/products/{pid}")).json()
            assert shadow.products[pid].stock == real["stock"] == initial_stock[pid]
        real_coupon = (await client.get(f"/shop/v1/coupons/{coupon_id}")).json()
        assert shadow.coupons[coupon_id].used == real_coupon["used"] == used_orders
        assert projected_total == real_total


async def test_intra_plan_coupon_double_use_preview_rejects(tmp_path):
    """create_coupon(max_uses=1) → create_order → create_order 必须预览拒绝第二次下单。"""
    await _assert_intra_plan_coupon_double_use_rejected(str(tmp_path / "shop.db"))


async def _assert_intra_plan_coupon_double_use_rejected(db_path: str) -> None:
    async with app_client(create_app(db_path)) as client:
        ctx = ToolContext(shop=client)
        products = (await client.get("/shop/v1/products")).json()
        before = build_shadow(products=products, coupons=[], orders=[])

        shadow = before.clone()
        steps = [
            ("create_coupon", {"code": "ONCE", "discount_pct": 20.0, "max_uses": 1}),
            ("create_order", {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": "preview-coupon-0"}),
            ("create_order", {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": "preview-coupon-0"}),
        ]
        preview_rejected = False
        for step, (tool, raw_args) in enumerate(steps):
            action = PlannedAction(tool=tool, args=raw_args)
            derived = derive_projection_args(action, shadow, step)
            try:
                assert_action_applicable(tool, derived, shadow)
                shadow = project(shadow, TOOL_SPECS, [(tool, derived)])
            except ProjectionError:
                preview_rejected = True
                break
        assert preview_rejected

        coupon = await handle(
            "create_coupon", {"code": "ONCE", "discount_pct": 20.0, "max_uses": 1}, ctx
        )
        real_coupon_id = coupon["coupon_id"]
        await handle(
            "create_order",
            {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": real_coupon_id},
            ctx,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await handle(
                "create_order",
                {"product_id": "p-tshirt-s", "qty": 1, "coupon_id": real_coupon_id},
                ctx,
            )
```

- [x] **Step 2: 跑测试**

Run: `uv run pytest tests/test_projection_consistency.py -v`
Expected: 4 passed

**如果属性测试失败，说明效果声明与真实执行的语义不一致——必须改实现，不能放宽断言。** 这是本计划唯一一条「失败即设计错误」的测试。

- [x] **Step 3: 跑全量测试与 lint**

Run: `uv run pytest -v && uv run ruff check .`
Expected: 全部通过，无 lint 错误

- [x] **Step 4: 手工验收（spec M1 的完成标志）**

终端一：

```bash
make shop
```

终端二：

```bash
make gateway
```

终端三：

```bash
curl -s -X POST localhost:8000/v1/sessions -H 'content-type: application/json' -d '{"agent_id":"pricing_agent","task_id":"t-demo"}'
```

拿到返回的 `session_id` 后：

```bash
curl -s -X POST localhost:8000/v1/plans/preview -H 'content-type: application/json' -d '{"session_id":"<上一步的 session_id>","actions":[{"tool":"update_price","args":{"product_id":"p-iphone","delta_pct":-20}}]}'
```

Expected: `metrics.gross_margin_pct_after` 低于 `gross_margin_pct_before`；`entities.p-iphone.list_price_cents` 为 479920。

再确认真实商城**未被改动**：

```bash
curl -s localhost:8100/shop/v1/products/p-iphone
```

Expected: `list_price_cents` 仍为 `599900`

- [x] **Step 5: Commit**

```bash
git add tests/test_projection_consistency.py
git commit -m "test: 投影一致性属性测试（spec §12.2 验收关卡）"
```

---

## 完成标志

本计划完成时，以下必须全部成立：

- [x] `uv run pytest -v` 全绿
- [x] `uv run ruff check .` 无错误
- [x] `curl` 能走通「建会话 → 预览计划 → 拿到终态指标」
- [x] 预览计划后真实商城数据**未被改动**
- [x] 属性测试 `test_projection_matches_real_execution` 通过

## 本计划明确不做的

- 任何策略判定（单次策略 / 组合风险 / 计划审批）→ 计划 ②③④
- 审计链、provenance、幂等 → 计划 ②
- 会话风险状态与预算 → 计划 ③
- 控制台 → 计划 ⑤
- 真实 Agent 与录屏场景 → 计划 ⑤
