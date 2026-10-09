# AI Agent 工具调用护栏 · 同类方案调研

> 调研日期：2026-10-03
> 调研方法：GitHub REST API、PyPI/npm/HuggingFace 元数据 API、抓取各项目 README/源码/文档原文
> 说明：本环境中 WebFetch 被网络策略拦截，改由 `gh` CLI、`curl`、`cdn.jsdelivr.net` 镜像抓取一手材料。凡抓不到的信息均标注「未找到」，不做推断。
> 阅读边界：调研用于识别共同约束、反例和设计机会，不作为任何项目的照搬模板。
> 正文会明确区分“公开实践”“设计判断”和“本项目选择”。

---

## 0. 调研结论

16 个项目大致分成三类形态。多数把判定停在单次工具调用；OpenAPPA 已做
跨调用数据流控制，Squidbrake 有跨步序列规则，但跨 Agent 组合仍然少见。

这一判断有同类项目文档的直接依据：

> "does not infer filenames from URLs, resolve variables or symlinks, inspect program contents, **or correlate separate tool calls**"
> —— [docs.rampart.sh/reference/threat-model](https://docs.rampart.sh/reference/threat-model/)

> "**Squidbrake doesn't know your intent**"
> —— [squidbrake/incidents/README.md](https://github.com/batrapulkit/squidbrake/blob/main/incidents/README.md)

---

## 1. 全景表

| 项目 | 形态 | 判定方式 | 许可 | Stars | 最近更新 | 可依赖程度 |
|---|---|---|---|---|---|---|
| [anthropics/commerce-agents](https://github.com/anthropics/commerce-agents) | 官方蓝图 | 代码级门（provenance / caps / stage-approve-apply） | Apache-2.0 | **3,139** | 2026-10-02 | 参考实现，**官方声明不维护、不收 PR**，跑虚构 ACME 数据 |
| [Shopify/claude-for-commerce-examples](https://github.com/Shopify/claude-for-commerce-examples) | 官方派生 | 同上，接真实 Shopify Storefront/Admin | Apache-2.0 | 142 | 2026-09-01 | 参考实现 |
| [Justin0504/Aegis](https://github.com/Justin0504/Aegis)（AEGIS） | 独立网关 | 22 条正则/模式 + JSON Schema 策略 DSL + 异常检测 | MIT | **471** | 2026-09-06 | 有论文 + 代码 + 商业化意图 |
| [peg/rampart](https://github.com/peg/rampart) | 本地策略防火墙 | YAML 规则（glob），无 LLM；LLM 可选 sidecar | Apache-2.0 | 89 | **2026-10-03** | 工程材料较完整：有威胁模型、策略测试、对抗性 bench |
| [preloop/preloop](https://github.com/preloop/preloop) | 控制平面 | YAML + CEL，**决策路径无 LLM** | Apache-2.0 | 71 | 2026-10-02 | 活跃，有 Cloud 商业版 |
| [fpytloun/intaris](https://github.com/fpytloun/intaris) | 自托管服务 | 本地规则 + **LLM 判定写操作** | **BSL 1.1（非开源）** | 22 | 2026-09-27 | Pre-Alpha，2030 才转 Apache |
| [batrapulkit/squidbrake](https://github.com/batrapulkit/squidbrake) | 审批网关 | 规则 + 污点分析 + **跨步序列**，无 LLM | Apache-2.0 | 11 | 2026-10-02 | 极新（4 天），但有 8 起真实事故重放测试 |
| [mindbomber/aana](https://github.com/mindbomber/Alignment-Aware-Neural-Architecture--AANA-) | 契约层 | 六路由契约 + 授权阶梯，确定性 | MIT | 3 | 2026-08-30 | Alpha Research，自证材料多、外部验证少 |
| [MNKIAgentOS/agent-trust](https://github.com/MNKIAgentOS/agent-trust)（mnki） | 框架内 | 身份 + 委派权限衰减 + 证据链 | Apache-2.0 | 1 | 2026-09-21 | 理论材料较完整，依赖重基础设施 |
| [sameerbhatt/portcullis](https://github.com/sameerbhatt/portcullis) | 框架内 | 可逆性 × 影响半径（人工声明） | MIT | 1 | 2026-07-23 停滞 | v0.1.0 原型 |
| [nbsgr/agentframework](https://github.com/nbsgr/agentframework)（coderun-agent） | 框架内 | `needsApproval` 工具名白名单 | MIT | 1 | 2026-09-20 | 1.0.x 版本号但两月龄 |
| [wolfpackaiagents/wolfpackai](https://github.com/wolfpackaiagents/wolfpackai) | 框架全家桶 | guardrails（PII/注入）+ 工具白名单 + 审批 | Apache-2.0 | 0 | 2026-10-02 | 数周龄，自称生产级 |
| [QuietFireAI/TelsonBase](https://github.com/QuietFireAI/TelsonBase) | MCP 网关 | 纯确定性规则 + 5 级信任分层 | MIT | 0 | 2026-08-03 停滞 | 自报 6,417 测试，**从未做第三方渗透测试** |
| [vh2225/habena](https://github.com/vh2225/habena) | 本地 MCP 代理 | 策略 + 预算 + 威胁检测，无 LLM | MIT | 0 | 2026-09-01 | 自述 **"Not yet recommended for production fleets"** |
| Guardian Agent SDK | SDK → 远端 | 服务端规则 + 可选 LLM judge | MIT | **仓库 404** | 2026-05-29 停更 | 无法验证 |
| sologate-langchain | 框架内 callback | 风险评分（**闭源**） | MIT | **仓库 404** | — | 商业 SaaS，策略逻辑不可见 |

### 需要修正的三处信息

1. Intaris 采用 Business Source License 1.1，2030-03-15 才转 Apache 2.0。
   PyPI 分类器标为 `Other/Proprietary License`，不能按开源软件处理。
2. Guardian Agent SDK 与 sologate 的源码仓库都返回 404，策略引擎、判定
   逻辑和审计不可见，尽调时不能采信其宣称。
3. AEGIS 的源码仓库已公开，摘要脚注给出 `github.com/Justin0504/Aegis`，
   MIT，471 stars，TypeScript。README 展示的能力超过论文，包括 14 个
   框架接入、Agent Threat Ontology、per-tenant Policy DSL 和 RFC 6962
   Merkle 审计日志。

---

## 2. 三个流派

### 流派 A：独立网关 / 控制平面（进程外）

代表项目有 Preloop、Rampart、Squidbrake、Intaris、TelsonBase、Habena 和
AEGIS。它们通常以 FastAPI/Starlette 服务加 MCP 代理或原生 hook 接入，
好处是收口集中、能跨会话聚合、审计统一，也不绑定 Agent 框架。

限制也很明确：它们只看到流量和请求体，拿不到被保护系统的业务状态，
因此很难判断“这一步在当前上下文里是否合理”。

### 流派 B：框架内集成（进程内）

代表项目有 portcullis、sologate、coderun-agent、Wolfpack、mnki 和 AANA。
它们包装 `BaseTool`、callback 或 in-loop 模块，能看到工具名、参数、授权
状态和会话状态，接入成本低。

代价是与框架强绑定。换框架后同一套护栏通常不能复用，各框架也容易形成
自己的小标准。

### 流派 C：官方蓝图

代表项目是 Anthropic 的 commerce-agents 和 Shopify 的派生版本。它们把
护栏放在工具调用内部，三条运行时共用同一个 executor，架构边界清楚。

这类方案只对特定部署、单进程和 Claude 工具协议成立，没有审计链、
RBAC 或合规报告，演示数据也是虚构的。它们适合作为实现参考，不适合
直接当生产系统。

---

## 3. 与本系统的设计差异

这一节不再按“别人缺什么”来写，而是把调研中反复出现的做法整理成七组
设计对照。左边是同类方案的常见选择，右边是本系统的选择；每组的差异都
对应后面的设计机制。

| 设计维度 | 同类方案常见做法 | 本系统 |
|---|---|---|
| 判定单位 | 单次调用为主，少数覆盖跨调用序列 | 会话与计划 |
| 业务语义 | 工具名、参数正则、动作类别 | 影子领域模型与结果态 |
| 审批对象 | 单次调用 payload | 计划、终态投影和批准后的动作哈希 |
| 策略验证 | 规则回归、单语料评测 | 威胁编号、双轴评测、确定性复现 |
| 决策路径 | 部分方案引入 LLM judge | 决策路径不使用 LLM |
| 覆盖边界 | 多数依赖接入完整性 | 同样依赖接入，但把它作为首要限制显式建模 |
| 审计语义 | 主要证明记录未被改写 | 哈希链加 fail-closed，并明确不证明写入内容为真 |

### 3.1 判定单位：从调用提升到会话

多数同类方案在一次工具调用到达时做判定，请求结束状态也结束。OpenAPPA
和 Squidbrake 已触到跨调用数据流或跨步序列，但这类能力仍不普遍。

本系统把判定单位提升到会话。会话保存 `(Δ, T, A)`，分别记录实体影响、
污点和动作序列。累积降价、发券后下单、读 PII 后外发都能在同一会话内
被识别。跨 Agent 场景再通过 `task_id` 和合并算子扩展到多个会话。

### 3.2 业务语义：从参数判断到结果态判断

语法层规则擅长拒绝 `rm -rf`、超阈值折扣这类明显违规，但无法回答“这笔
退款在当前业务状态下是否合理”。同类方案多数停在语法层，或依赖使用者
自行补充领域规则。

本系统在网关侧维护影子领域模型。订单、优惠券、价格和库存的变更先经过
投影，风险规则读取的是结果态，而不是模型自报的参数。金额上限、毛利
变化和成本线击穿因此可以进入判定。

### 3.3 审批对象：从动作变成计划

按动作审批在单步场景里很直接，但 Agent 清仓 30 个商品时会变成 30 次
点击。审批人看到的是零散请求，很难判断整件事会落到什么状态。

本系统要求 Agent 先提交计划。网关对计划做终态投影，把动作列表、影响
范围和业务指标放在同一个审批视图里；批准后使用动作哈希逐项执行，参数
被改动或计划状态变化都会在重新求值时暴露。

### 3.4 策略验证：从规则回归到双轴评测

策略测试常见的是“规则修改后有没有回归”，这能发现误伤，却不能说明
策略是否覆盖真实威胁。Rampart 的 bench 和 Squidbrake 的事故重放已经
接近更完整的做法，但多数项目仍只覆盖其中一半。

本系统把对抗语料、正常语料和确定性测试放在同一条验证链上：对抗集看
拦截率，正常集看误伤率，威胁编号看覆盖面，双栈执行看同一动作序列是否
得到同一决策。评测结果由 `make eval` 和 CI 门禁固定下来。

### 3.5 决策路径：LLM 不参与最终判定

部分方案把高风险写操作交给 LLM 做安全评估，或在外围挂一个 LLM judge。
这类设计能表达复杂语义，但裁判本身也可能被提示注入。

本系统的决策路径保持确定性。系统本身不依赖 LLM 生成计划才能工作；
外部 Agent 可以用 LLM 生成计划、解释结果或辅助写策略，但不能直接决定
allow / deny。最终判断由策略、投影、合并算子和状态机完成，错误可以被
复现。

### 3.6 覆盖边界：不把接入完整性当成安全证明

同类方案和本系统都无法阻止完全绕开网关的 Agent。差别在于是否把这条
边界说清楚，以及是否让所有受保护动作经过同一个入口。

本系统把所有接入形态收口到同一判定链，MCP、LangChain 和 HTTP 共享
工具注册表、策略和审计。覆盖完整性仍写在 `docs/limitations.md` 的第一
条，不把“接进来了”写成“系统安全”。

### 3.7 审计语义：证明没被改，不伪装成证明内容为真

哈希链能发现记录被篡改，但不能证明写入时的内容一定真实。部分项目把
审计、签名和可信时间混在一起讲，容易让读者误以为它解决了事实来源问题。

本系统只承诺可验证的边界：审计链检查顺序和内容是否被改写，策略和投影
失败时 fail-closed。审批身份当前记录在审计理由文本中，动作哈希保存在
计划 token 和计划记录里；两者尚未作为结构化字段写入审计 entry。尾部
截断和写入真实性这些边界单独列在 limitations 中。

---

## 4. 失败语义上的设计分歧

| 项目 | 未匹配任何规则时 | 网关不可达时 |
|---|---|---|
| Preloop | **放行（fail-open）** | — |
| Habena | 拒绝（隐式拒绝为地板） | 拒绝 |
| Squidbrake | `default: review`（扣住等人） | 拒绝（fail-closed） |
| Rampart | 按边界类型区分 | 原生 hook fail-closed / wrap 可配 fail-open |

**Preloop 的 fail-open 把“漏配规则”直接变成安全洞。** 这是必须明确表态的设计决策，不能含糊。

---

## 5. 可借鉴的四个公开实践（来自 Anthropic 蓝图）

Anthropic 的 commerce-agents 虽然不维护，但架构边界很清晰。以下四项
适合作为设计参考，而不是照搬实现。

1. **“安全是代码，不是提示词”与单点 executor**
   所有门放在工具调用内部，三条运行时共用 `execute()`。换运行时底线不松。
   独立层需要找到所有 tool call 必经的 choke point。

2. **Provenance gates（来源门）**
   写操作只接受“本会话工具返回过的 id”。这是对抗间接注入最有效的一条：
   被注入的模型无法凭空生成真实系统里的 id 去改价。
   实现位置：`shopping-agent/core/shopping_agent/gates.py` 的 `check_provenance()`。

3. **stage → approve → apply + apply 时重校验**
   staging 只写 `ChangeLedger`，不发任何 mutation；apply 时**再跑一次** `check_guardrails()`（防 staging 后配置收紧）。
   这里有个容易漏掉的点：在聊天里打“批准”不产生批准标记。

4. **Caps validated against resulting state 与服务端渲染受保护字段**
   上限要对结果态计算，不采信模型宣称值；价格、费用、政策和结账 URL
   由服务端填入，模型只报 id。这一条部分解决了累加型风险，也是现有
   项目里少数触及组合效应的地方。

---

## 6. 不宜直接照搬的三处设计

1. 词表和正则是 per-domain 且脆弱的。`grounding.py` 的 terms+cues、
   product id 正则都是 ACME 特有的。通用层应改为策略即数据
   （policy-as-data）。
2. **Provenance 状态存在示例宿主里**（`examples/demo_common/sessions.py`），不是包的一部分，且只保留最新 N 条。需要**分布式、带 TTL 的 provenance store**。
3. Grounding 保证会按运行时衰减。Messages API 全支持，Agent SDK 只有
   部分规则，Managed Agents 一条都没有。通用层不能接受“某些路径没覆盖”。

---

## 7. 结论：设计机会在哪里

三条候选，按推荐排序：

### 设计机会 1：组合风险护栏（跨调用 / 跨 Agent）

设计机会在于把护栏单位从调用提升到会话或意图。

- 16 个项目全不做；Rampart 在官方威胁模型里亲口承认缺口
- 有真实事故背书：Squidbrake 的重放集里就有「先关备份 → 再删库」这种**每步单独看都合法**的序列
- 跨 Agent 组合仍少见（mnki 的委派衰减、AANA 的 Daybreak 是最接近的尝试，都只是试点/单点）

技术难点是既要跨调用有状态，又不能退化成“什么都扣住给人看”的审批疲劳。

### 设计机会 2：意图级审批（批准「计划」而非「动作」）

设计机会在于把 Agent 接下来的 N 步聚合成一个可撤销的计划，让人一次
批准整件事，而不是点 N 次同意。

- 所有项目都是逐动作审批，**没有一个把"这一串整体要干什么"呈现给人**
- 和缝隙 1 天然互补：组合风险检测的产出，正好就是"计划"的边界
- demo 效果极好：左边是 Agent 的 30 步计划，右边是"这一次改动会让毛利变成 -12%"

### 设计机会 3：业务语义护栏

设计机会在于让判定读取“这次改动 + 当前状态”的合成结果，包括毛利、
库存、叠加优惠和订单生命周期。

- 现有项目全是语法层判定
- Anthropic 的 "caps against resulting state" 解决了数量类的累计，价格类、组合类的没解决
- 缺点：需要领域建模，工作量更大，且容易做成"又一套规则引擎"

---

## 8. 对方案的影响

原方案（独立的写操作护栏网关）本身仍然成立，但需要重新定位：

| 原设计 | 需要改成 |
|---|---|
| opaque token 防幻觉 ID | **降级为标配**（Anthropic 已做，不是差异化） |
| 哈希链审计 | **降级为标配**（Preloop/Squidbrake/TelsonBase/AEGIS 都有） |
| allow/deny/ask 三态 | **降级为标配**（所有人都有） |
| 幂等 + 金额上限 | **降级为标配** |
| — | 本项目核心：跨调用/跨 Agent 组合风险 |
| — | 本项目核心：意图级审批（批准计划） |
| — | 本项目核心：策略本身的可测试性（语义层，不只是回归） |

**设计切口**：系统不把自己定位成又一个逐调用判定的护栏网关，而是把
判定单位提升到会话和计划。前者回答“这一下能不能做”，后者回答“这一串
做完会怎样”。

---

## 附录：抓取时的项目指标

抓取时点 2026-10-03。

| 项目 | 许可 | Stars | 最后 push | 版本 | 有测试 |
|---|---|---|---|---|---|
| anthropics/commerce-agents | Apache-2.0 | 3,139 | 2026-10-02 | — | 有 |
| Justin0504/Aegis | MIT | 471 | 2026-09-06 | — | 有 |
| peg/rampart | Apache-2.0 | 89 | 2026-10-03 | v1.9.x | 有（含对抗 bench） |
| preloop/preloop | Apache-2.0 | 71 | 2026-10-02 | v0.16.0 | 有（含 fuzz/codeql） |
| fpytloun/intaris | BSL 1.1 | 22 | 2026-09-27 | v0.11.0 | 有 |
| batrapulkit/squidbrake | Apache-2.0 | 11 | 2026-10-02 | v0.3.9 | 有（事故重放） |
| mindbomber/aana | MIT | 3 | 2026-08-30 | Alpha | 有 |
| MNKIAgentOS/agent-trust | Apache-2.0 | 1 | 2026-09-21 | 0.3.0 | 有 |
| sameerbhatt/portcullis | MIT | 1 | 2026-07-23 | 0.1.0 | 有 |
| nbsgr/agentframework | MIT | 1 | 2026-09-20 | 1.0.8 | 有 |
| wolfpackaiagents/wolfpackai | Apache-2.0 | 0 | 2026-10-02 | 0.2.5 | 有 |
| QuietFireAI/TelsonBase | MIT | 0 | 2026-08-03 | v12.0.0（文档标 v11.0.3，自相矛盾） | 有 |
| vh2225/habena | MIT | 0 | 2026-09-01 | 0.4.0 | 有 |
| Guardian Agent SDK | MIT | 仓库 404 | 2026-05-29 | 1.0.2 | ✖ |
| sologate-langchain | MIT | 仓库 404 | — | 0.2.0 | ✖ |

从 star 数和项目年龄看，头部项目之外的多数仓库仍处于原型阶段。


---

## 9. 同类方案格局扩展与设计主张（2026-10 调研复核）

> 原调研（§1-§8）聚焦 commerce agent 护栏小圈子的 16 个项目。经两轮扩展搜索
> （产品名关键词 → awesome 聚合清单 → 信任/治理语义关键词 → 中文生态，四轴），
> 累计覆盖 25+ 个项目。本节合并两轮复查结论，作为差异主张的最终依据。

### 9.1 范围边界声明

本项目判定层在**工具调用序列**。以下三类不构成直接同层方案，可叠加使用：

- **LLM I/O 护栏框架**（litellm 60k★、portkey 13k★、bifrost 8.6k★、
  guardrails-ai 7.5k★、NeMo Guardrails 7.3k★）管模型输入输出，
  不管工具调用序列
- **MCP 网关基础设施**（IBM/mcp-context-forge 4.6k★、katanemo/plano 7.1k★）
  负责流量收口与注册，护栏可挂在其后
- **评测/观测平台**（future-agi 2.1k★ 等）

### 9.2 扩展发现（同层方案与相邻项目）

| 项目 | Stars | 建于 | 许可 | 与本项目的关系 |
|---|---|---|---|---|
| [microsoft/agent-governance-toolkit](https://github.com/microsoft/agent-governance-toolkit) | **6,414** | 2026-03-02 | MIT | **第一跟踪对象。** 三语言 SDK、OWASP Agentic Top 10 映射（7F+3P，README badge 原文）+ EU AI Act 映射、`agt verify` 合规校验 CLI（内置 policy linting）、多框架适配；其 ACS spec 有标签流 IFC（自称 stateless）与 approval resolver，但无数值组合风险/预算阶梯/序列规则（原文核验见 §9.5） |
| [archestra-ai/OpenAPPA](https://github.com/archestra-ai/OpenAPPA) | **1,504** | 2026-08-18 | MIT | 跨调用数据流控制（APPA 策略代数，TOML 声明式策略；「污点追踪」系业界术语概括，原文为追踪读入内容的敏感度与信任级）；确定性决策（事件日志唯一输入）+ NeurIPS 2026 Agents in the Wild workshop + 双轴 benchmark（Bench-Corp 20 工作流 / AgentThreatBench——托管于英国政府 BEIS 的 inspect_evals），与本项目 T 机制最接近（原文核验见 §9.5） |
| [luckyPipewrench/pipelock](https://github.com/luckyPipewrench/pipelock) | 921 | 2026-02-08 | Apache-2.0 | agent firewall：MCP 安全 + egress 出口控制 |
| [AI45Lab/AgentDoG](https://github.com/AI45Lab/AgentDoG) | 700 | — | — | 学术派 diagnostic guardrail framework |
| [statewright/statewright](https://github.com/statewright/statewright) | 505 | 2026-05-03 | 自定义 | 状态机护栏，是「会话状态」的另一种表达 |
| [cordum-io/cordum](https://github.com/cordum-io/cordum) | 509 | 2026-01-11 | BUSL 1.1（LICENSE 原文确认） | Agent Control Plane：job 级 REQUIRE_APPROVAL 审批（按 job 标签触发）+ safety kernel（gRPC）+ Cordum Edge（Claude Code 本地合规防火墙）（原文核验见 §9.5） |
| [invariantlabs-ai/invariant](https://github.com/invariantlabs-ai/invariant) | 467 | 2024-05-08 | Apache-2.0 | agent 安全分析（推送停于 2026-01） |
| [archestra-ai/archestra](https://github.com/archestra-ai/archestra) | 4,355 | — | 自定义 | 带护栏的企业 AI 平台 |

原 16 个项目（§1）状态稳定：stars 微涨、无结构性变化。

### 9.3 威胁排序与四个可验证的设计主张

排序：**微软 ACS > OpenAPPA > cordum > 其余相邻层**。

三个最接近的先行者也各有明确缺口：

- **微软 ACS**：引擎自称 stateless（逐次求值），跨调用状态靠宿主自担；
  「projection」是工具目录元数据投影，非业务终态投影
- **OpenAPPA**：管数据流向（污点），不管业务数值的合成结果（成本线击穿）
- **cordum**：job 级审批按标签触发，无投影终态呈现与批准后哈希校验

据此，四个差异化主张（经两轮独立搜索仍成立）：

1. **数值组合风险**（累积降价 × 叠加优惠 × 成本线击穿，multiplicative/additive/max 算子）
2. **风险预算阶梯**（deny/ask/flag 三段渐变刹车）
3. **跨 Agent 合并算子**（multiplicative 叠加 + 单贡献者守卫）
4. **计划级投影审批**（终态呈现给人 + plan_token 三重校验）

风险：若微软后续给 ACS 加有状态会话层，1/2 会被压掉。差异化必须持续落在
**业务语义深度**（电商领域模型、成本线、毛利投影），通用平台最难顺手覆盖。

### 9.4 调研复核机制

- 搜索必须跨四轴：产品名 / 语义能力 / 聚合清单（awesome-llm-security 等）/
  语言生态。单一关键词轴有系统性盲区（微软项目就是第二轮才抓到的）
- 每月复查一次，发现新威胁先更新本文档再动代码

### 9.5 头部项目原文核验记录

对三个头部项目的关键声明做了 spec/README/LICENSE **原文逐条核验**
（GitHub API 拉取原文，非二手转述），结论：主要声明成立，另修正两处
表述并补充 3 个细节。外部原文未随仓库保存，当前不能从仓库独立复验。

| 声明 | 核验结果 |
|---|---|
| 微软 ACS 引擎 stateless | ✅ spec 原文 6 处：「The runtime is stateless. It MUST NOT retain mutable state that influences a verdict」 |
| 微软无 cumulative/sequence/budget/会话状态 | ✅ 全文各 0 处 |
| 微软 projection = 工具目录投影 | ✅ 唯一一处：「Catalog of tools used for projection, defined in section 9」 |
| 微软 OWASP 7F+3P | ✅ README badge 原文「OWASP Agentic Top 10: 7 Full, 3 Partial」；**补充：另有 EU AI Act 映射与 `agt verify` 合规校验 CLI（内置 policy linting，与本项目 `make lint` 的策略 lint 同类）** |
| OpenAPPA 确定性 + 可复现 | ✅ 原文：「check is deterministic and returns the same decision on every run」「the same log always gets the same decision」——与本项目可复现性测试（B3）同一性质主张 |
| OpenAPPA「污点追踪」 | ⚠️ 原文无 taint 一词；准确表述为「追踪读入内容的敏感度与信任级，逐调用做数据流控制」（APPA 策略代数，TOML 策略）。「污点追踪」系安全领域术语概括，语义成立 |
| cordum BUSL 1.1 | ✅ LICENSE 文件头原文（GitHub 标 NOASSERTION 因 BUSL 不在自动识别列表） |
| cordum job 级审批 / safety kernel / Edge | ✅ REQUIRE_APPROVAL 按 job 标签触发；safety kernel 独立 gRPC 服务；Cordum Edge 为 Claude Code 合规防火墙 |
| AgentThreatBench 出处 | ⚠️ 补充：非 OpenAPPA 自有，托管于英国政府 BEIS 的 inspect_evals 仓库 |

核验带来的一个新对照点：**微软与本项目都内置 policy linting**——策略文件
静态引用校验不是本项目独创，但其 `agt verify` 面向 OWASP 合规检查，本项目
`policy-lint` 面向「策略引用的工具/实体是否存在于注册表」，深度不同但同属
「策略可测试性」叙事，对外表述避免声称独创。

### 9.6 Bench-Corp 全量处置结果（B4 二期）

20 个场景全部处置（不凑数）：**14 个完整转译 + 1 个仅覆盖 attack 路径**
（dual-control-wire 的 OK 路径无法表达），共计 `eval/cases_bench_corp.yaml`
29 case；primary 28 实测拦截 100% / 误伤 0%，`make eval-bench`。另有
**5 个 skipped 并归因**（受众粒度 4 + 前置条件断言 1，见 limitations #15/#16）。
3 个 known overreach case（读论坛后建正常工单等）如实保留被拦——
暴露污点源粗粒度（读任何敏感/不可信内容即锁死写路径）的真实代价。
关键发现：**Bench-Corp 的受众级控制（audience narrowing）是 APPA 的核心
机制，也是本项目与 OpenAPPA 的真实能力差距所在**——转译比引用数字更
诚实地说清了这一点。
