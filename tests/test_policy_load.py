from __future__ import annotations

import textwrap

import pytest

from guardrail.policy.lint import CAP_FIELDS, lint_policy
from guardrail.policy.loader import load_policy
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
    thresholds = [r for r in policy.rules_for("create_coupon") if r.deny_if is not None]
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

    # 刻意手工拼行而不用 textwrap.dedent：dedent 剥掉公共缩进后，
    # f-string 插进来的多行片段（permissions_yaml）会保留自己的缩进，
    # 拼出非法 YAML。这里让每段都从第 0 列开始，缩进完全显式。
    document = "\n".join(
        [
            "version: 1",
            "permissions:",
            permissions_yaml or _all_tools_permissions(),
            "rules:",
            textwrap.dedent(rules_yaml).strip(),
        ]
    )
    return SingleCallPolicy.model_validate(yaml.safe_load(document))


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
            deny_if: "args.x > 1"
            message: m
          - id: b
            match: {tool: update_price}
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


def test_shipped_policy_cap_is_in_cents():
    # 结果层上限的单位是整数分：50000 分 = 500.00 元，不是 5 万元。
    p = load_policy(REPO_POLICY)
    caps = [r for r in p.rules if r.cap_field is not None]
    assert [r.cap_field for r in caps] == ["total_amount_cents"]
    assert caps[0].max == 50000
