"""策略 lint 测试（spec §12.4：make lint 的策略半边）。"""

from pathlib import Path

import yaml

from guardrail.policy_lint import lint_policies


def test_real_policies_pass():
    assert lint_policies() == []


def test_unknown_tool_in_permissions_is_caught(tmp_path: Path):
    bad = tmp_path / "single_call.yaml"
    base = yaml.safe_load(Path("policies/single_call.yaml").read_text(encoding="utf-8"))
    base["permissions"]["ops_agent"] = [*base["permissions"].get("ops_agent", []),
                                        "teleport"]
    bad.write_text(yaml.dump(base, allow_unicode=True), encoding="utf-8")
    problems = lint_policies(tmp_path)
    assert any("teleport" in p for p in problems)


def test_unknown_tool_in_cost_classes_is_caught(tmp_path: Path):
    bad = tmp_path / "combined_risk.yaml"
    base = yaml.safe_load(
        Path("policies/combined_risk.yaml").read_text(encoding="utf-8"))
    base["budget"]["costs"]["cost_classes"]["sensitive_write"].append("nuke")
    bad.write_text(yaml.dump(base, allow_unicode=True), encoding="utf-8")
    problems = lint_policies(tmp_path)
    assert any("nuke" in p for p in problems)


def test_missing_policy_file_is_caught(tmp_path: Path):
    problems = lint_policies(tmp_path)
    assert len(problems) >= 3  # 三份策略全缺
