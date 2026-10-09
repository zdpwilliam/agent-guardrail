import httpx
import pytest
from asgi_lifespan import LifespanManager

from guardrail.clock import now_iso
from guardrail.config import Settings
from guardrail.idempotency import idempotency_key
from guardrail.main import create_app
from guardrail.stores.approvals import SqliteApprovalStore
from guardrail.stores.sqlite import SqliteBackend
from shop.main import create_app as create_shop_app


@pytest.fixture
async def store(tmp_path):
    backend = SqliteBackend(str(tmp_path / "gateway.db"))
    await backend.connect()
    yield SqliteApprovalStore(backend)
    await backend.close()


def _pa(id: str = "pa-1", tool: str = "update_price", cost: float = 0.15,
        session_id: str = "s-1"):
    from guardrail.stores.approvals import PendingApproval

    return PendingApproval(
        id=id, session_id=session_id, agent_id="pricing_agent", tool=tool,
        args={"product_id": "p-iphone", "delta_pct": -5.0},
        reasons=["预算 ≤ 0.10，扣下等人审批"], cost=cost, created_at=now_iso(),
    )


# ---------- 存储 ----------


async def test_create_and_load(store):
    await store.create(_pa())
    loaded = await store.load("pa-1")
    assert loaded is not None
    assert loaded.tool == "update_price"
    assert loaded.cost == 0.15
    assert loaded.resolved_at is None


async def test_load_missing_returns_none(store):
    assert await store.load("pa-nope") is None


async def test_resolve_approve_is_atomic(store):
    await store.create(_pa())
    first = await store.resolve("pa-1", "approve", "boss", "同意")
    assert first is not None
    assert first.resolution == "approve"
    assert first.decided_by == "boss"
    assert first.resolved_at is not None


async def test_double_resolve_returns_none(store):
    # 并发 resolve 只有一个赢家：重复处理一张审批单必须被原子挡下。
    await store.create(_pa())
    assert await store.resolve("pa-1", "approve", "boss") is not None
    assert await store.resolve("pa-1", "reject", "other") is None


async def test_resolve_missing_returns_none(store):
    assert await store.resolve("pa-nope", "approve", "boss") is None


async def test_list_open_excludes_resolved(store):
    await store.create(_pa("pa-1"))
    await store.create(_pa("pa-2", tool="get_product", cost=0.0))
    await store.resolve("pa-1", "reject", "boss")
    open_ids = [p.id for p in await store.list_open()]
    assert open_ids == ["pa-2"]


# ---------- API：批准后执行 / 拒绝释放 ----------


@pytest.fixture
async def gateway(tmp_path):
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


async def _prime(client, sid):
    r = await client.post("/v1/tools/list_products", json={"session_id": sid, "args": {}})
    assert r.status_code == 200


async def test_resolve_approve_executes_and_deducts(gateway, client):
    sid = (await client.post(
        "/v1/sessions", json={"agent_id": "pricing_agent", "task_id": "t-1"}
    )).json()["session_id"]
    await _prime(client, sid)

    # 模拟 authorize 产生的 ASK：占位幂等键 + 待审批单。
    args = {"product_id": "p-iphone", "delta_pct": -5.0}
    key = idempotency_key(sid, "update_price", args)
    assert await gateway.state.idempotency.begin(key, sid) is True
    await gateway.state.approvals.create(_pa("pa-exec", cost=0.15, session_id=sid))

    r = await client.post(
        "/v1/approvals/pa-exec/resolve",
        json={"resolution": "approve", "decided_by": "boss", "comment": "同意"},
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "allow"

    # 商城生效 + 预算扣减 + 幂等完成。
    price = (await client.post(
        "/v1/tools/get_product", json={"session_id": sid, "args": {"product_id": "p-iphone"}}
    )).json()["result"]["product"]["list_price_cents"]
    assert price == round(599900 * 0.95)
    state, _ = await gateway.state.sessions.load_state(sid)
    assert state.risk_budget == pytest.approx(0.85)
    cached = await gateway.state.idempotency.get(key)
    assert cached is not None and cached.status == "done"
    assert cached.response["replayed"] is False


async def test_approve_after_session_expiry_does_not_execute(gateway, client):
    sid = (await client.post(
        "/v1/sessions", json={"agent_id": "pricing_agent", "task_id": "t-1"}
    )).json()["session_id"]
    await _prime(client, sid)

    args = {"product_id": "p-iphone", "delta_pct": -5.0}
    key = idempotency_key(sid, "update_price", args)
    assert await gateway.state.idempotency.begin(key, sid) is True
    await gateway.state.approvals.create(_pa("pa-expired", session_id=sid))
    await gateway.state.backend.execute(
        "UPDATE sessions SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", sid),
    )
    await gateway.state.backend.commit()

    before = (await gateway.state.shop.get("/shop/v1/products/p-iphone")).json()[
        "list_price_cents"
    ]
    r = await client.post(
        "/v1/approvals/pa-expired/resolve",
        json={"resolution": "approve", "decided_by": "boss"},
    )

    assert r.status_code == 403
    assert "会话" in r.json()["detail"]
    after = (await gateway.state.shop.get("/shop/v1/products/p-iphone")).json()[
        "list_price_cents"
    ]
    assert after == before
    assert await gateway.state.idempotency.get(key) is None


async def test_resolve_reject_releases_key(gateway, client):
    sid = (await client.post(
        "/v1/sessions", json={"agent_id": "pricing_agent", "task_id": "t-1"}
    )).json()["session_id"]
    await _prime(client, sid)
    args = {"product_id": "p-iphone", "delta_pct": -5.0}
    key = idempotency_key(sid, "update_price", args)
    await gateway.state.idempotency.begin(key, sid)
    await gateway.state.approvals.create(_pa("pa-rej", session_id=sid))

    r = await client.post(
        "/v1/approvals/pa-rej/resolve",
        json={"resolution": "reject", "decided_by": "boss"},
    )
    assert r.status_code == 200
    assert r.json()["decision"] == "rejected"
    # 幂等键释放：同一调用可以真正重试（走正常判定链）。
    assert await gateway.state.idempotency.get(key) is None


async def test_double_resolve_returns_409(client, gateway):
    sid = (await client.post(
        "/v1/sessions", json={"agent_id": "pricing_agent", "task_id": "t-1"}
    )).json()["session_id"]
    await _prime(client, sid)
    pa = await gateway.state.approvals.create(_pa("pa-dup", session_id=sid))
    body = {"resolution": "approve", "decided_by": "boss"}
    r1 = await client.post(f"/v1/approvals/{pa.id}/resolve", json=body)
    assert r1.status_code == 200
    r2 = await client.post(f"/v1/approvals/{pa.id}/resolve", json=body)
    assert r2.status_code == 409


async def test_list_open_endpoint(client, gateway):
    sid = (await client.post(
        "/v1/sessions", json={"agent_id": "pricing_agent", "task_id": "t-1"}
    )).json()["session_id"]
    await _prime(client, sid)
    await gateway.state.approvals.create(_pa("pa-open", session_id=sid))
    r = await client.get("/v1/approvals")
    assert r.status_code == 200
    assert [p["id"] for p in r.json()] == ["pa-open"]


async def test_unknown_approval_404(client):
    r = await client.post(
        "/v1/approvals/pa-nope/resolve",
        json={"resolution": "approve", "decided_by": "boss"},
    )
    assert r.status_code == 404
