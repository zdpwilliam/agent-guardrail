from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PolicyError(Exception):
    """策略缺失、无法解析或不通过自检。

    进程应当拒绝启动（spec §10.2）：一个没加载上策略的网关不是「宽松的网关」，
    是一个不知道自己在干什么的网关。
    """


class ToolMatch(BaseModel):
    tool: str


class SingleRule(BaseModel):
    """一条单次策略规则。

    两种形态互斥：

    - **语法层**：只给 `deny_if`，读参数即可判定（spec §6.1 第 6 步）。
    - **结果层**：给 `cap_field` + `max` + `compute_from`，从投影结果态读值
      （spec §6.3）。

    刻意不做「两者都声明」或「两者都不声明」——那两种配置都会让规则的真实
    语义变成「取决于解析顺序」，所以直接在模型层拒绝。
    """

    id: str
    match: ToolMatch
    message: str
    deny_if: str | None = None
    cap_field: str | None = None
    max: int | None = None
    compute_from: Literal["resulting_state"] | None = None

    @model_validator(mode="after")
    def _exactly_one_form(self) -> SingleRule:
        has_syntax = self.deny_if is not None
        has_result = self.cap_field is not None
        if has_syntax == has_result:
            raise ValueError(f"规则 {self.id!r} 必须且只能声明 deny_if 或 cap_field 之一")
        if has_result:
            if self.max is None or self.compute_from is None:
                raise ValueError(
                    f"规则 {self.id!r} 的结果层形态必须同时声明 max 与 compute_from"
                )
        elif self.max is not None or self.compute_from is not None:
            raise ValueError(f"规则 {self.id!r} 的语法层形态不应声明 max / compute_from")
        return self


class SingleCallPolicy(BaseModel):
    version: int = 1
    permissions: dict[str, list[str]] = Field(default_factory=dict)
    rules: list[SingleRule] = Field(default_factory=list)
    # 本策略覆盖的工具清单（多域时按域声明）；空 = 覆盖全部注册工具。
    # lint 的「每工具必须有人授权」检查只对 covers 内的工具生效。
    covers: list[str] = Field(default_factory=list)

    def rules_for(self, tool: str) -> list[SingleRule]:
        return [rule for rule in self.rules if rule.match.tool == tool]

    def is_permitted(self, agent_id: str, tool: str) -> bool:
        """未列出的 agent 没有任何权限。

        这是刻意的 fail-closed：新增一个 agent 必须显式授权，而不是继承全量。
        反过来（默认全放行）意味着任何一次漏配都直接等价于越权。
        """
        return tool in self.permissions.get(agent_id, [])
