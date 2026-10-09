"""corp 域（v0.3）：企业文件域的工具注册与内存态。

第二领域验证 spec §18.1 的主张——「网关必须知道自己保护的是什么领域」
是一个显式、可控的成本：本文件就是这个成本的全部（7 个工具规格 +
一个内存态）。判定代码零改动。

- send_email 不在这里：发邮件的动作只有一个，电商域与 corp 域复用
  registry 里的同名工具（corp_email_fanout 规则用的 email_delta 风险
  增量也声明在那边）。
- 隔离靠策略权限表：ecommerce 策略对 corp_agent 之外的 agent 不授 corp
  工具，fail-closed 拒绝；corp 策略（policies/corp_*.yaml）只授权 corp_agent。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from guardrail.tools.registry import TOOL_SPECS, EntityEmit, ToolSpec


@dataclass
class CorpState:
    """corp 域的内存态：企业文件系统 + 邮件 + 论坛 + 工单 + 电汇日志。

    B4 第二阶段（Bench-Corp 全量处置）扩了三个系统：public_forum（公开
    出口）、task_tracker（内部工单）、wire（电汇）。hr/finance/vendor
    统一表示为 files 的路径前缀——它们在 Bench-Corp 里都是「读敏感内容」
    的源系统，域差异不影响判定。
    """

    files: dict[str, str] = field(default_factory=dict)
    sent: list[dict[str, str]] = field(default_factory=list)
    forum_posts: list[dict[str, str]] = field(default_factory=list)
    tickets: list[dict[str, str]] = field(default_factory=list)
    wires: list[dict[str, str]] = field(default_factory=list)


CORP_TOOL_SPECS: dict[str, ToolSpec] = {
    "list_files": ToolSpec(
        name="list_files",
        kind="read",
        args_schema={"type": "object", "properties": {},
                     "additionalProperties": False},
        emits=[EntityEmit(entity_type="file", path="files[].path")],
    ),
    "read_file": ToolSpec(
        name="read_file",
        kind="read",
        taint_source=True,
        taint_categories=["corp_pii"],
        args_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
        emits=[EntityEmit(entity_type="file", path="file.path")],
    ),
    "read_forum": ToolSpec(
        name="read_forum",
        kind="read",
        # 论坛帖是不可信内容（可能含注入指令），粗粒度归入 corp_pii 污点
        # 类别——读它之后的外传动作与读敏感文件同受 corp_data_exfiltration
        # 约束（follow-forum-steps / inspect-forum-post 的攻击形态）。
        taint_source=True,
        taint_categories=["corp_pii"],
        args_schema={
            "type": "object",
            "properties": {
                "forum": {"type": "string", "minLength": 1},
                "thread": {"type": "string", "minLength": 1},
            },
            "required": ["forum", "thread"],
            "additionalProperties": False,
        },
        emits=[EntityEmit(entity_type="file", path="thread.path")],
    ),
    "post_forum": ToolSpec(
        name="post_forum",
        kind="write",
        args_schema={
            "type": "object",
            "properties": {
                "forum": {"type": "string", "minLength": 1},
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["forum", "title", "body"],
            "additionalProperties": False,
        },
    ),
    "create_ticket": ToolSpec(
        name="create_ticket",
        kind="write",
        args_schema={
            "type": "object",
            "properties": {
                "tracker": {"type": "string", "minLength": 1},
                "title": {"type": "string", "minLength": 1},
                "body": {"type": "string"},
            },
            "required": ["tracker", "title", "body"],
            "additionalProperties": False,
        },
        emits=[EntityEmit(entity_type="file", path="ticket.title")],
    ),
    "execute_wire": ToolSpec(
        name="execute_wire",
        kind="write",
        args_schema={
            "type": "object",
            "properties": {
                "request_file": {"type": "string", "minLength": 1},
                "amount_cents": {"type": "integer", "minimum": 1},
                "beneficiary_account": {"type": "string", "minLength": 1},
            },
            "required": ["request_file", "amount_cents", "beneficiary_account"],
            "additionalProperties": False,
        },
    ),
    "write_file": ToolSpec(
        name="write_file",
        kind="write",
        args_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        emits=[EntityEmit(entity_type="file", path="file.path")],
        # 无 effects：corp 语料不走计划投影（via=call），写文件的影子终态
        # 没有消费者——留空是诚实的最小声明，不是遗漏。
    ),
}

TOOL_SPECS.update(CORP_TOOL_SPECS)
