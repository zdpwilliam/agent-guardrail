from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
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


class ShopStore:
    """商城仓储。所有写操作都是单条原子的 UPDATE/INSERT。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("ShopStore.connect() 未调用")
        return self._conn

    async def ping(self) -> None:
        await self.conn.execute("SELECT 1")

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

    async def update_price(self, product_id: str, delta_pct: float) -> ProductRow:
        product = await self.get_product(product_id)
        if product is None:
            raise NotFoundError(f"商品不存在: {product_id}")
        target_cents = product.list_price_cents * (1 + delta_pct / 100)
        if not math.isfinite(target_cents):
            # NaN / ±Inf 或溢出（如 delta_pct=1e305）会让 round() 抛 ValueError /
            # OverflowError → 500。执行必须至少和预览一样安全（预览对此返回 400）。
            raise InvalidStateError(f"改价计算溢出为非有限值: delta_pct={delta_pct}")
        new_price = round(target_cents)
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


def _to_product(row: aiosqlite.Row) -> ProductRow:
    return ProductRow(
        id=row["id"],
        name=row["name"],
        category=row["category"],
        cost_price_cents=row["cost_price_cents"],
        list_price_cents=row["list_price_cents"],
        stock=row["stock"],
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
