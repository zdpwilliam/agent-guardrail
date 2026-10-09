from datetime import datetime

import pytest

from guardrail.config import Settings
from guardrail.main import create_app
from tests.conftest import app_client


async def test_session_ttl_uses_injected_settings(tmp_path):
    app = create_app(
        Settings(
            gateway_db_path=str(tmp_path / "gateway.db"),
            session_ttl_minutes=1,
        )
    )

    async with app_client(app) as client:
        r = await client.post(
            "/v1/sessions",
            json={"agent_id": "ops_agent", "task_id": "ttl-check"},
        )
        assert r.status_code == 200
        body = r.json()
        created = datetime.fromisoformat(body["created_at"])
        expires = datetime.fromisoformat(body["expires_at"])
        assert (expires - created).total_seconds() == pytest.approx(60)
