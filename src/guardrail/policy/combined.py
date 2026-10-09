from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

# Δ 的字段闭集（spec §3.2 的 EntityDelta）。组合规则的 field / combine.a/b 都只能
# 引用这里列出的字段——lint 据此抓拼写错误，Task 2 的 EntityDelta 模型字段必须与之
# 一致（有专门测试钉住）。
DELTA_FIELDS: tuple[str, ...] = (
    "price_delta_pct", "stock_delta", "coupon_rate_delta", "email_delta",
)

class CostConfig(BaseModel):
    sensitive_write: float
    normal_write: float
    warn_surcharge: float
    # 分类名 → 工具列表。lint 强制全部工具恰分类一次。
    cost_classes: dict[str, list[str]]


class ThresholdConfig(BaseModel):
    deny_below: float
    ask_at_or_below: float
    flag_at_or_below: float


class BudgetConfig(BaseModel):
    initial: float
    costs: CostConfig
    thresholds: ThresholdConfig


class CumulativeRule(BaseModel):
    """累积型（spec §3.4①）：同一实体的同类字段被反复修改，单次合规、累计越界。"""

    type: Literal["cumulative"]
    id: str
    severity: Literal["warn", "deny"]
    # 刻意用 str 而不是 Literal：拼错的字段要在 lint 里带着规则 id 报出来
    #（「规则 'c' 引用了未知的 Δ 字段：margin_delta_pct」），而不是一条
    # 没有上下文的 pydantic ValidationError。
    field: str
    warn_at: float | None = None
    deny_at: float | None = None
    message: str


class SequenceStep(BaseModel):
    tool: str
    where: str | None = None


class SequenceRule(BaseModel):
    """序列型（spec §3.4②）：有序动作模式，每步单独看都合法。"""

    type: Literal["sequence"]
    id: str
    severity: Literal["warn", "deny"]
    steps: list[SequenceStep]
    max_gap: int
    message: str


class TaintRule(BaseModel):
    """污点型（spec §3.4③）：污点从源传播到汇。"""

    type: Literal["taint"]
    id: str
    severity: Literal["warn", "deny"]
    sources: list[str]
    source_taint: str
    sinks: list[str]
    sink_condition: str | None = None
    message: str


class CrossAgentCombine(BaseModel):
    # 同 CumulativeRule.field：字段名留给 lint 报错，模型不拦。
    a: str
    b: str
    # custom 在模型层放行、在 lint 层拒绝：M3 不实现注册命名函数
    # （那是一个新的逃逸面），但保留字段让策略文件报错时指向明确的补齐路径。
    op: Literal["add", "multiplicative", "max", "custom"]


class CrossAgentRule(BaseModel):
    """跨 Agent 型（spec §3.4④）：多 Agent 各自合规的动作合起来击穿底线。"""

    type: Literal["cross_agent"]
    id: str
    severity: Literal["warn", "deny"]
    contributors: list[str]
    combine: list[CrossAgentCombine]
    max_combined: float
    message: str


Rule = Annotated[
    CumulativeRule | SequenceRule | TaintRule | CrossAgentRule,
    Field(discriminator="type"),
]


class CombinedPolicy(BaseModel):
    version: int = 1
    covers: list[str] = Field(default_factory=list)
    budget: BudgetConfig
    rules: list[Rule] = Field(default_factory=list)

    def rules_of(
        self, rule_type: str
    ) -> list[CumulativeRule | SequenceRule | TaintRule | CrossAgentRule]:
        return [r for r in self.rules if r.type == rule_type]


# ---------- 求值器（spec §3.4） ----------
# 四类规则读的是同一个会话三元组 (Δ, T, A) 的不同投影（spec §3.6）。
# 新增一类风险 = 加一个投影，不是加一套历史扫描逻辑。

from guardrail.clock import now_iso  # noqa: E402
from guardrail.models import EntityDelta, SessionState, ToolSpec  # noqa: E402
from guardrail.policy.expr import ExpressionError, evaluate  # noqa: E402
from guardrail.policy.merge import combine_values, merge_entities  # noqa: E402


class RuleHit(BaseModel):
    rule_id: str
    severity: Literal["warn", "deny"]
    message: str


class CombinedVerdict(BaseModel):
    hits: list[RuleHit] = Field(default_factory=list)
    deny: bool = False
    warn_count: int = 0
    reasons: list[str] = Field(default_factory=list)

    @property
    def rule_ids(self) -> list[str]:
        return [h.rule_id for h in self.hits]


def apply_risk_deltas(
    spec: ToolSpec, args: dict, entities: dict[str, EntityDelta]
) -> tuple[dict[str, EntityDelta], set[str]]:
    """把本次调用声明的增量并入 Δ 的**内存副本**，返回 (pending, touched)。

    - 实体 key 缺参时跳过该声明（没带券就不该动券，与效果声明语义一致）。
    - 求值异常抛 ExpressionError，调用方 fail-closed——Δ 记不出来就不能
      声称「这次调用没有累积效应」。
    - touched 只含本次声明过的实体：累积规则只查它们。不做这个限制，
      「会话里商品 A 已越界、此时读一次商品列表」也会对这次**零成本读**
      报 warn 并加成本——语义完全错误。
    """
    pending = {k: d.model_copy(deep=True) for k, d in entities.items()}
    touched: set[str] = set()
    for decl in spec.risk_deltas:
        raw = args.get(decl.entity_arg)
        if raw is None or raw == "":
            continue
        key = f"{decl.entity_type}:{raw}"
        value = evaluate(decl.value_expr, args)
        delta = pending.setdefault(
            key, EntityDelta(entity_key=key, last_updated_at=now_iso())
        )
        try:
            setattr(delta, decl.field, getattr(delta, decl.field) + value)
        except TypeError as exc:
            # 声明表达式算出了不能相加的值（如模型把 delta_pct 传成字符串）。
            # Δ 记不出来就不能继续——fail-closed，由调用方拒绝本次调用。
            raise ExpressionError(
                f"风险增量 {decl.field!r} 计算失败：{exc}"
            ) from exc
        delta.last_updated_at = now_iso()
        touched.add(key)
    return pending, touched


def _exceeds(value: float, threshold: float) -> bool:
    """「超过」的严格语义，方向随阈值符号。恰在阈值上不算超过——这决定了
    场景 2 里第 4 次 -5%（累计恰 -20）不触发 warn。"""
    if threshold < 0:
        return value < threshold
    return value > threshold


def evaluate_cumulative(
    rules: list, pending: dict[str, EntityDelta], touched: set[str]
) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for rule in rules:
        for key in sorted(touched):
            value = getattr(pending[key], rule.field)
            # 命中严重度由**越过哪条线**决定，不是规则的静态 severity：
            # 越过 deny_at 就是 deny，越过 warn_at 就是 warn。规则的
            # severity 字段只是声明意图，跨线判定不能被它降级。
            if rule.deny_at is not None and _exceeds(value, rule.deny_at):
                hits.append(RuleHit(rule_id=rule.id, severity="deny",
                                    message=rule.message))
                break
            if rule.warn_at is not None and _exceeds(value, rule.warn_at):
                hits.append(RuleHit(rule_id=rule.id, severity="warn",
                                    message=rule.message))
                break  # 同一规则对多实体只报一次，成本别按实体数翻倍
    return hits


def _matches(step: object, tool: str, args: dict) -> bool:
    if step.tool != tool:
        return False
    if step.where is None:
        return True
    try:
        return bool(evaluate(step.where, args))
    except ExpressionError:
        # where 是**匹配条件**，不是安全条件：求值失败按「此步不匹配」处理。
        # 安全条件的 fail-closed 在污点汇那里，取舍相反——见 evaluate_taint。
        return False


def evaluate_sequence(
    rules: list, actions: list, tool: str, args: dict
) -> list[RuleHit]:
    """在 A 的尾部 + 当前调用里按序匹配 steps。

    当前调用是最后一个候选（它还没进 A）：「发券 → 现在下单」的第二步
    就是本次调用。相邻两步之间最多间隔 max_gap 个动作。
    """
    hits: list[RuleHit] = []
    candidates = [(a.tool, a.args) for a in actions] + [(tool, args)]
    for rule in rules:
        first = -1
        matched = 0
        for idx, (c_tool, c_args) in enumerate(candidates):
            if matched >= len(rule.steps):
                break
            step = rule.steps[matched]
            if _matches(step, c_tool, c_args):
                if matched > 0 and (idx - first - 1) > rule.max_gap:
                    break
                if matched == 0:
                    first = idx
                matched += 1
                if matched == len(rule.steps):
                    hits.append(RuleHit(rule_id=rule.id, severity=rule.severity,
                                        message=rule.message))
                    break
        # 贪心首匹配可能把 step1 定得太早导致 step2 落在 gap 外；demo 场景
        # （两步、gap 3）足够。多步规则的回溯留给真实需求出现时。
    return hits


def evaluate_taint(
    rules: list, taint: set[str], tool: str, args: dict
) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for rule in rules:
        if rule.source_taint not in taint:
            continue
        if tool not in rule.sinks:
            continue
        if rule.sink_condition is None:
            hits.append(RuleHit(rule_id=rule.id, severity=rule.severity,
                                message=rule.message))
            continue
        try:
            fired = bool(evaluate(rule.sink_condition, args))
        except ExpressionError:
            # 与 sequence 的 where 取舍**相反**：sink_condition 是安全条件，
            # 守不住就当要外发——fail-closed（spec §10.2）。
            fired = True
        if fired:
            hits.append(RuleHit(rule_id=rule.id, severity=rule.severity,
                                message=rule.message))
    return hits


def evaluate_cross_agent(
    rules: list,
    agent_id: str,
    task_id: str | None,
    pending: dict[str, EntityDelta],
    siblings: list[tuple[object, SessionState]],  # (身份记录[含 agent_id], 状态)
) -> list[RuleHit]:
    """同 task_id 会话快照合并后做 combine（spec §3.4④/§3.7）。

    - 读快照不要求全局一致（§3.7.2 的明确取舍，代价是极小概率漏判）。
    - pending 是**本次调用**的贡献，只算一次——自己已提交的 Δ 在快照里，
      不能重复计。
    - contributors 过滤：调用者不在名单里时该规则对它不生效。
    - **至少 2 个贡献者有非零增量才评估**：跨 Agent 规则管的是「叠加」，
      单方越界是累积规则的职责。没有这个门槛，一张 60% 的券会单独把
      multiplicative 算成 -60 而误触发（单测验证过这个 bug）。
    - siblings 元素是 (SessionRecord, SessionState)：agent_id 在身份行，
      SessionState 刻意不携带（单一事实来源）。解包顺序曾写反过——
      单测当时也传反了所以没拦住，修正后两边同步钉死。
    """
    if task_id is None:
        return []
    hits: list[RuleHit] = []
    for rule in rules:
        if agent_id not in rule.contributors:
            continue
        combine_fields = {entry.a for entry in rule.combine} | {
            entry.b for entry in rule.combine
        }
        merged = {k: d.model_copy(deep=True) for k, d in pending.items()}
        contributing: set[str] = set()
        if any(
            getattr(d, field) != 0 for d in pending.values() for field in combine_fields
        ):
            contributing.add(agent_id)
        for rec, st in siblings:
            if rec.agent_id not in rule.contributors:
                continue
            merged = merge_entities(merged, st.entities)
            if any(
                getattr(d, field) != 0
                for d in st.entities.values()
                for field in combine_fields
            ):
                contributing.add(rec.agent_id)
        if len(contributing) < 2:
            continue
        value = combine_values([c.model_dump() for c in rule.combine], merged)
        # 含等：max_combined 是成本线，达到即击穿（场景 4 的 -26.4 ≤ -25）。
        if value <= rule.max_combined:
            hits.append(RuleHit(rule_id=rule.id, severity=rule.severity,
                                message=rule.message))
    return hits


def evaluate_combined(
    policy: CombinedPolicy,
    state: SessionState,
    agent_id: str,
    task_id: str | None,
    spec: ToolSpec,
    tool: str,
    args: dict,
    siblings: list[tuple[SessionState, str]] | None = None,
) -> tuple[CombinedVerdict, dict[str, EntityDelta], set[str]]:
    """对一次调用做组合风险求值。返回 (verdict, pending Δ, touched)。

    pending/touched 随 verdict 一起返回：授权段算一遍，生效提交段直接续用，
    同一请求内 Δ 只构建一次。
    """
    pending, touched = apply_risk_deltas(spec, args, state.entities)
    hits: list[RuleHit] = []
    hits += evaluate_cumulative(policy.rules_of("cumulative"), pending, touched)
    hits += evaluate_sequence(policy.rules_of("sequence"), state.actions, tool, args)
    hits += evaluate_taint(policy.rules_of("taint"), state.taint, tool, args)
    hits += evaluate_cross_agent(
        policy.rules_of("cross_agent"), agent_id, task_id, pending, siblings or []
    )
    deny = any(h.severity == "deny" for h in hits)
    warn_count = sum(1 for h in hits if h.severity == "warn")
    verdict = CombinedVerdict(
        hits=hits, deny=deny, warn_count=warn_count,
        reasons=[h.message for h in hits],
    )
    return verdict, pending, touched
