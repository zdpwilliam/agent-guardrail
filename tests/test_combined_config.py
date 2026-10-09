import textwrap

import pytest

from guardrail.policy.combined import DELTA_FIELDS, CombinedPolicy
from guardrail.policy.lint import lint_combined_policy
from guardrail.policy.loader import load_combined_policy
from guardrail.tools.registry import TOOL_SPECS

REPO_COMBINED = "policies/combined_risk.yaml"


def _write(tmp_path, text: str) -> str:
    path = tmp_path / "combined_risk.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


# ---------- 仓库里那份策略本身 ----------


def test_shipped_combined_policy_loads_and_lints():
    policy = load_combined_policy(REPO_COMBINED)
    assert policy.version == 1
    lint_combined_policy(policy, TOOL_SPECS)


def test_shipped_policy_has_all_four_rule_types():
    policy = load_combined_policy(REPO_COMBINED)
    types = {r.type for r in policy.rules}
    assert types == {"cumulative", "sequence", "taint", "cross_agent"}


def test_budget_coefficients_are_configurable_not_hardcoded():
    policy = load_combined_policy(REPO_COMBINED)
    assert policy.budget.initial == 1.0
    assert policy.budget.costs.sensitive_write == 0.15
    assert policy.budget.costs.normal_write == 0.05
    assert policy.budget.costs.warn_surcharge == 0.20
    assert policy.budget.thresholds.ask_at_or_below == 0.10
    assert policy.budget.thresholds.flag_at_or_below == 0.30


def test_delta_fields_are_the_canonical_set():
    assert set(DELTA_FIELDS) == {"price_delta_pct", "stock_delta",
                                "coupon_rate_delta", "email_delta"}


# ---------- 加载 fail-closed ----------


def test_missing_file_raises(tmp_path):
    with pytest.raises(Exception, match="不存在"):
        load_combined_policy(str(tmp_path / "nope.yaml"))


def test_unparsable_yaml_raises(tmp_path):
    with pytest.raises(Exception, match="无法解析"):
        load_combined_policy(_write(tmp_path, "rules: [\n  - id: x\n bad"))


def test_non_mapping_root_raises(tmp_path):
    with pytest.raises(Exception, match="顶层必须是映射"):
        load_combined_policy(_write(tmp_path, "- a\n- b\n"))


def test_missing_budget_section_raises(tmp_path):
    with pytest.raises(Exception, match="结构非法"):
        load_combined_policy(_write(tmp_path, "version: 1\nrules: []\n"))


# ---------- lint ----------


def _rules(text: str) -> str:
    """调用处的规则片段带源码缩进，FULL_BUDGET 是零缩进——两者必须各自 dedent
    再拼接，整体 dedent 会因为公共前缀为 0 而失效（M2 Task 4 同一个坑）。"""
    return textwrap.dedent(text)


def _policy(yaml_text: str) -> CombinedPolicy:
    import yaml

    raw = yaml.safe_load(textwrap.dedent(yaml_text))
    return CombinedPolicy.model_validate(raw)


FULL_BUDGET = """
budget:
  initial: 1.0
  costs:
    sensitive_write: 0.15
    normal_write: 0.05
    warn_surcharge: 0.20
    cost_classes:
      sensitive_write: [update_price, create_coupon]
      normal_write: [update_stock, create_order, refund_order, send_email]
      read: [list_products, get_product, get_order]
  thresholds:
    deny_below: 0.0
    ask_at_or_below: 0.10
    flag_at_or_below: 0.30
"""


def test_lint_rejects_duplicate_rule_id():
    p = _policy(
        FULL_BUDGET
        + _rules("""
        rules:
          - id: dup
            type: cumulative
            severity: warn
            field: price_delta_pct
            warn_at: -20.0
            message: m
          - id: dup
            type: cumulative
            severity: warn
            field: stock_delta
            warn_at: -100
            message: m
        """)
    )
    with pytest.raises(Exception, match="重复"):
        lint_combined_policy(p, TOOL_SPECS)


def test_lint_rejects_unknown_tool_in_sequence_step():
    p = _policy(
        FULL_BUDGET
        + _rules("""
        rules:
          - id: s
            type: sequence
            severity: deny
            steps:
              - {tool: teleport}
            max_gap: 3
            message: m
        """)
    )
    with pytest.raises(Exception, match="teleport"):
        lint_combined_policy(p, TOOL_SPECS)


def test_lint_rejects_unknown_field_in_cumulative():
    p = _policy(
        FULL_BUDGET
        + _rules("""
        rules:
          - id: c
            type: cumulative
            severity: warn
            field: margin_delta_pct
            warn_at: -20.0
            message: m
        """)
    )
    with pytest.raises(Exception, match="margin_delta_pct"):
        lint_combined_policy(p, TOOL_SPECS)


def test_lint_rejects_cumulative_without_any_threshold():
    p = _policy(
        FULL_BUDGET
        + _rules("""
        rules:
          - id: c
            type: cumulative
            severity: warn
            field: price_delta_pct
            message: m
        """)
    )
    with pytest.raises(Exception, match="至少声明"):
        lint_combined_policy(p, TOOL_SPECS)


def test_lint_rejects_sequence_where_typo():
    # args.delt_pct 拼错——与单次策略 lint 同样的抓法。
    p = _policy(
        FULL_BUDGET
        + _rules("""
        rules:
          - id: s
            type: sequence
            severity: deny
            steps:
              - {tool: update_price, where: "abs(args.delt_pct) > 5"}
              - {tool: create_order}
            max_gap: 3
            message: m
        """)
    )
    with pytest.raises(Exception, match="delt_pct"):
        lint_combined_policy(p, TOOL_SPECS)


def test_lint_rejects_taint_source_that_is_not_a_source_tool():
    p = _policy(
        FULL_BUDGET
        + _rules("""
        rules:
          - id: t
            type: taint
            severity: deny
            sources: [list_products]
            source_taint: pii
            sinks: [send_email]
            sink_condition: "email_domain(args.to) != 'internal.corp'"
            message: m
        """)
    )
    with pytest.raises(Exception, match="污点源"):
        lint_combined_policy(p, TOOL_SPECS)


def test_lint_rejects_custom_operator():
    p = _policy(
        FULL_BUDGET
        + _rules("""
        rules:
          - id: x
            type: cross_agent
            severity: deny
            contributors: [pricing_agent, marketing_agent]
            combine:
              - {a: price_delta_pct, b: coupon_rate_delta, op: custom}
            max_combined: -25.0
            message: m
        """)
    )
    with pytest.raises(Exception, match="custom"):
        lint_combined_policy(p, TOOL_SPECS)


def test_lint_rejects_unknown_field_in_combine():
    p = _policy(
        FULL_BUDGET
        + _rules("""
        rules:
          - id: x
            type: cross_agent
            severity: deny
            contributors: [pricing_agent, marketing_agent]
            combine:
              - {a: margin_pct, b: coupon_rate_delta, op: multiplicative}
            max_combined: -25.0
            message: m
        """)
    )
    with pytest.raises(Exception, match="margin_pct"):
        lint_combined_policy(p, TOOL_SPECS)


def test_lint_rejects_tool_in_two_cost_classes():
    p = _policy(
        FULL_BUDGET.replace(
            "normal_write: [update_stock, create_order, refund_order, send_email]",
            "normal_write: [update_stock, create_order, refund_order, send_email, update_price]",
        )
        + "rules: []\n"
    )
    with pytest.raises(Exception, match="update_price"):
        lint_combined_policy(p, TOOL_SPECS)


def test_lint_rejects_unclassified_tool():
    p = _policy(
        FULL_BUDGET.replace(
            "read: [list_products, get_product, get_order]",
            "read: [list_products, get_product]",
        )
        + "rules: []\n"
    )
    with pytest.raises(Exception, match="get_order"):
        lint_combined_policy(p, TOOL_SPECS)


def test_lint_rejects_non_monotonic_thresholds():
    p = _policy(
        FULL_BUDGET.replace("flag_at_or_below: 0.30", "flag_at_or_below: 0.05")
        + "rules: []\n"
    )
    with pytest.raises(Exception, match="单调"):
        lint_combined_policy(p, TOOL_SPECS)
