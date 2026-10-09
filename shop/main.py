from __future__ import annotations

from collections.abc import AsyncIterator
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
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = ShopStore(resolved_db)
        await store.connect()
        await store.init_schema()
        await store.seed_demo_data()
        app.state.store = store
        yield
        await store.close()

    app = FastAPI(title="迷你商城（内部）", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> dict:
        await request.app.state.store.ping()
        return {"status": "ok"}

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
