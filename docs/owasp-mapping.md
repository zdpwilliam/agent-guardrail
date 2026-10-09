# OWASP Agentic Top 10 合规映射

> 依据 OWASP Agentic Security Initiative 公开的 Agentic AI Threats &
> Mitigations（T1-T10）。条目名以官方最新版本为准；本映射基于 2026-10 抓取的
> 草稿条目。逐条给「覆盖机制 / 状态 / 边界注记」，**不凑数**——
> 状态只有 Full（机制直接覆盖该威胁的主要攻击面）与 Partial（覆盖一部分，
> 剩余面见注记）。

| 条目 | 威胁 | 本项目覆盖机制 | 状态 | 边界注记 |
|---|---|---|---|---|
| T1 Memory Poisoning | 污染 Agent 的记忆/上下文，让后续决策被毒化 | T 污点追踪（读过的 PII/密钥持久化为污点，后续外发被拦）+ Provenance 门（写操作只能作用于本会话工具返回过的实体，凭空捏造的引用直接拒绝） | Partial | Agent 自身的对话记忆不在护栏范围内——护栏管工具调用序列，不管模型内部状态 |
| T2 Tool Misuse | 合法工具被用于错误目的 | 单次策略（白名单/权限/阈值）+ 四类组合风险（序列/污点/累积/跨 Agent）——对抗集 20 组中 16 组属于此类，拦截率 100% | **Full** | 覆盖以 eval 语料（`eval/cases_adversarial.yaml` 的 `threat_refs`）为准；未编入语料的滥用模式不做承诺 |
| T3 Privilege Compromise | 越权、权限提升 | 权限矩阵 fail-closed（未列出的 agent 没有任何权限）+ plan_token「批准的是这一个计划，不是任意授权」（哈希封存 + 三重校验） | **Full** | 身份由调用方声明（spec §19.1 接缝），无密码学身份验证 |
| T4 Resource Overload | 资源耗尽、成本失控、分批累计超限 | 风险预算阶梯（deny/ask/flag 三段渐变刹车）+ 结果态上限（从投影终态计算，不信模型宣称值）+ 幂等防重复消耗 | **Full** | 预算系数经 `make sweep` 小网格验证，语料扩大后需重扫 |
| T5 Cascading Hallucination | 幻觉输出被下一环采信并放大 | 结果态上限不信 args 信终态 + Provenance 门拒绝幻觉生成的实体 id（对抗注入最有效的一条） | Partial | 抑制幻觉**后果**（幻觉 id 到不了商城、夸大改动过不了上限）；不检测幻觉本身 |
| T6 Intent Breaking & Goal Manipulation | 目标被篡改、意图被劫持 | `intent` 字段纯展示、绝不参与判定（spec §4.2，防注入污染判定）；组合风险只看客观增量 | Partial | 目标篡改发生在模型内部，护栏无法观测；能保证的是「无论意图如何被劫持，客观后果过不了组合风险」 |
| T7 Misaligned / Deceptive Behaviors | 行为偏离授权意图、隐蔽违规 | 序列规则（发高折扣券→带券下单）与污点规则（读订单→外发）抓「行为序列的客观形态」，不管意图 | Partial | 行为级覆盖；意图级对齐超出确定性护栏的边界（也避开「LLM 判 LLM」悖论，spec §12 盲区 5） |
| T8 Repudiation & Transparency | 抵赖、不可追溯 | 审计哈希链（篡改可检）+ 审计时间线（每次决策含理由/规则 id）+ `/metrics` 按决策与原因计数 + 计划审计（tool 记为 plan:{id}） | **Full** | 边界：哈希链只证「未被改」，不证「写入时为真」；尾部截断需外部锚点（limitations #3）——与所有同类项目相同的结构性边界 |
| T9 Identity & Trust Abuse | 多身份合谋、信任链滥用 | 跨 Agent 合并算子（同一 task_id 下多 Agent 贡献合并求值）+ 单贡献者守卫 + 合谋叠加击穿成本线即拦（对抗集 ADV-011..013） | **Full** | 合谋以共享 task_id 为前提——这是部署约定（spec §18.2），不防身份伪造本身 |
| T10 Shadow Agents | 未治理的 Agent/旁路接入 | 商城只接受网关请求（网络边界）+ 三条接入路径（MCP/LangChain/HTTP）全部走同一判定链 | Partial | **接入完整性不可验证**（limitations #2）——Agent 绕开网关直连则无防护。这是所有同类项目的结构性问题，本项目靠「商城仅内网 + 唯一凭证在网关」缓解 |

## 汇总

**5 Full（T2/T3/T4/T8/T9）+ 5 Partial（T1/T5/T6/T7/T10）。**

Full 的判定标准：本项目有专门的机制（而非附带效果）直接覆盖该威胁的主要
攻击面，且有对抗语料或单测证明。Partial 的注记全部指向文档化的边界
（`docs/limitations.md`），不留未声明的缺口。

对齐的对抗语料：`eval/cases_adversarial.yaml` 每条带 `threat_refs`
（OWASP:T1-T10 / MITRE ATLAS:AML.Txxxx），`make eval` 输出各条目覆盖组数。
