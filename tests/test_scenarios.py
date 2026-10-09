"""demo 场景测试（spec §13：4 个场景程序化跑通）。"""

import pytest

from guardrail.demo import print_results, run_scenarios


@pytest.fixture
async def client(tmp_path):
    from guardrail.demo import build_demo_stack

    async with build_demo_stack(str(tmp_path / "demo.db")) as (_app, c):
        yield c


async def test_all_four_scenarios_pass(client):
    results = await run_scenarios(client)
    assert print_results(results) is True
    names = [r["name"] for r in results]
    assert len(names) == 4
    assert all("场景" in n for n in names)


async def test_scenario_steps_carry_details(client):
    results = await run_scenarios(client)
    for r in results:
        assert r["steps"], r["name"]
        for s in r["steps"]:
            assert s["desc"] and s["detail"]
