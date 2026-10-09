# 会话级风险护栏 · agent-guardrail

会话级风险护栏网关。**其他护栏管「这一下能不能做」，本项目管「这一串做完会怎样」。**

一个被保护的迷你商城 + 一个护栏网关。Agent 只能通过网关操作商城，永远拿不到商城的直接凭证。

## 设计判断：会话状态不能被压成单次调用

同一套语料（`eval/`，42 组，全部标注 `synthetic: true`）跑三个系统：

| 系统 | 对抗集拦截率 | 正常集误伤率 |
|---|---|---|
| 无护栏 | 10%（下界） | 14%* |
| 单次判定基线（逐调用判定） | 25% | 14%* |
| **本项目** | **100%** | **0%** |

无护栏一行的误伤来自商城自身的业务校验（负库存等），不是护栏——这正是「拦截能力有下界、难在拦截时不误伤」的含义。

复现：`make eval`（输出各 OWASP 条目的对抗语料覆盖数）；第二领域（文件/邮件，语料 sourced 自 Bench-Corp 场景语义）见 `make eval-corp` ——其对抗集全部为组合型攻击（每步合规），单次判定 0% 是面对这类攻击的真实水平，与电商域 25%（含单次违规语料）的构成不同、不可直接并排。

Bench-Corp 20 场景已全量处置（`make eval-bench`，14 完整转译 + 1 仅 attack 路径 + 5 skipped 归因），其中暴露的受众粒度缺口如实记录于 limitations。语料路径与标注见 `eval/`，合规映射见 [docs/owasp-mapping.md](docs/owasp-mapping.md)（5 Full / 5 Partial，逐条给边界）。

**拦截率 ≠ 安全**——语料是自编的，真实攻击者的创造力不会止步于此；完整边界见 [docs/limitations.md](docs/limitations.md)。

设计差异来自两层状态：**会话状态三元组 (Δ, T, A)** 与 **跨 Agent 合并算子**。单次判定只能拦 25%（语法层违规），累积降价、发券即下单、PII 外发、跨 Agent 叠加击穿——这些每步单独看都合规的序列，只有组合风险层能拦。

同类方案调研覆盖 16+ 个项目，（详见 [同类方案调研 §9](docs/research/01-agent-guardrail-competitive-analysis.md)）：最接近的先行者是 OpenAPPA（跨调用数据流污点，1.5k★）与 cordum（job 级审批），但**数值组合风险、预算阶梯、跨 Agent 合并、计划级投影审批**四项仍无人做。

## 现在能跑什么

当前版本是 **v1.2.0 单节点生产基线**。它把判定链、审批链和审计链做成可部署的闭环：API key 与审批人身份、控制台登录和 CSRF、请求体限额与限流、逐行 JSON 日志、健康探针、SQLite WAL、PVC、审计脱敏与外部锚点、保留期清理，以及带 TLS 的 K8s 清单都已落地。

这里所说的生产可用，边界是**单节点、单 worker、可备份恢复**。多副本共享 SQLite、跨区域高可用、策略热加载和审批通知不在当前版本内；这些边界在 [部署说明](deploy/README.md)和 [limitations](docs/limitations.md)中明确列出。

```bash
make demo       # 一键起 商城+网关+控制台，浏览器开 http://127.0.0.1:8000/console
make scenarios  # 顺序跑完 4 个演示场景并打印每步决策
make eval       # 三方基线对比（无护栏 / 单次判定 / 本项目），打印拦截率与误伤率表
make sweep      # 风险预算系数小网格扫描（spec §18.3）
make verify     # 校验审计哈希链完整性
make audit-anchor  # 输出链头锚点，供外部日志保存
make maintenance   # 按保留期清理终态业务记录，不截断审计链
make bench      # 单会话延迟与并发吞吐基线
make up         # Docker Compose 容器化版本
```

或分终端手起：

```bash
make shop      # 终端一：迷你商城（仅内网）
make gateway   # 终端二：护栏网关（控制台在 http://127.0.0.1:8000/console）
```

```bash
# 建会话
curl -X POST localhost:8000/v1/sessions \
  -H 'content-type: application/json' -d '{"agent_id":"ops_agent","task_id":"t-demo"}'

# 先读一眼 —— Provenance 门要求写操作的目标必须在本会话读到过
curl -X POST localhost:8000/v1/tools/list_products \
  -H 'content-type: application/json' -d '{"session_id":"<sid>","args":{}}'

# 把 iPhone 打一折 → 403「单次降价不得超过 10%」
curl -X POST localhost:8000/v1/tools/update_price \
  -H 'content-type: application/json' \
  -d '{"session_id":"<sid>","args":{"product_id":"p-iphone","delta_pct":-90}}'

# 提交一份计划 → 控制台一次批准，凭 plan_token 逐步执行
curl -X POST localhost:8000/v1/plans \
  -H 'content-type: application/json' \
  -d '{"session_id":"<sid>","intent":"清仓","actions":[{"step":0,"tool":"update_price","args":{"product_id":"p-iphone","delta_pct":-8.0}}]}'

# 审计链完整性 / 可观测性
curl localhost:8000/v1/audit/verify
curl localhost:8000/readyz
curl localhost:8000/metrics
```

外部 Agent 接入：MCP stdio（`src/guardrail/mcp_server.py`）与 LangChain
duck-typed 工具（`src/guardrail/langchain_tools.py`），均走同一条判定链。

## 生产配置

默认 demo 不要求 API key。生产部署必须完整配置控制台密钥、审批人身份和
两组 API key：

```bash
export GUARDRAIL_ENV=production
export GUARDRAIL_CONSOLE_KEY='<long-random-console-key>'
export GUARDRAIL_CONSOLE_APPROVER_ID='security_lead'
export GUARDRAIL_AGENT_API_KEYS='{"agent-secret":"ops_agent"}'
export GUARDRAIL_APPROVER_API_KEYS='{"approver-secret":"security_lead"}'
```

请求使用 `X-API-Key` 或 `Authorization: Bearer <key>`。启用后，agent key
只能创建和操作绑定 `agent_id` 的会话，approver key 才能审批，且
审批人身份取自 key 映射，不信任请求体里的 `decided_by`。控制台使用
`HttpOnly + Secure + SameSite=Strict` cookie 和 CSRF token；外部入口必须
由 Ingress 或负载均衡终止 TLS。

## 已实现的能力

| 层 | 能力 | 章节 | 状态 |
|---|---|---|---|
| 标配 | 单次策略（YAML 策略即数据、受限表达式求值） | spec §6.1 | ✅ |
| 标配 | 结果态上限（金额从投影终态读，不信 args） | spec §6.3 | ✅ |
| 标配 | 审计哈希链（含篡改检测） | spec §7 | ✅ |
| 标配 | Provenance 门 | spec §8 | ✅ |
| 标配 | 幂等 | spec §6.2 | ✅ |
| 标配 | 三态决策中的 `allow` / `deny` | spec §10.1 | ✅ |
| 标配 | fail-closed 失败语义 | spec §10.2 | ✅ |
| **核心** | **会话状态 (Δ, T, A) + 风险预算 + 决策阶梯** | spec §3.2/§3.3 | ✅ |
| **核心** | **四类组合风险（累积/序列/污点/跨 Agent）** | spec §3.4 | ✅ |
| **核心** | **跨 Agent 合并算子 + 乐观锁** | spec §3.7 | ✅ |
| **核心** | **计划级审批 + plan_token + 生命周期** | spec §4 | ✅ |
| 基础 | 商城 + 工具层 + 网关侧效果声明 + 投影 | spec §4.3 | ✅ |
| 使能 | 控制台（预算条 / 计划卡片 / 审计时间线） | spec §9 | ✅ |
| 使能 | Agent demo 模式（4 场景可程序化复现） | spec §13 | ✅ |
| 差异化 | 评测语料 + 三方基线对比 + 系数扫描 | spec §12.3 | ✅ |
| 工程 | Docker Compose + CI eval 门禁 + CLI | spec §19.5 | ✅ |
| 工程 | `/healthz` + `/metrics` + `/readyz`（OTel 见局限） | spec §19.4 | ✅ |
| 工程 | MCP + LangChain 接入适配器 | spec §19.3 | ✅ |
| 生产 | API key、控制台 CSRF、限流、请求体限额、JSON 日志 | spec §19.7 | ✅ |
| 生产 | SQLite WAL + PVC、审计脱敏、保留期、外部锚点 | spec §19.7 | ✅ |

## 场景复现

| 场景 | 复现 | 预期 |
|---|---|---|
| 1：单次阈值 | `update_price` -90% | 403 `[max_single_price_cut]`，未触达商城 |
| 2：预算耗尽 | 同会话连续 6 次 `update_price`（-3.0 递变到 -3.5），第 7 次 -3.6 | 第 6 次 `allow_with_flag`，第 7 次 **202 + pending_approval_id**；批准后执行并扣减，透支后读也被拒 |
| 3a：序列违规 | `create_coupon 60%` → 紧接带券下单 | 403 `[coupon_self_purchase]`（两步各自合规） |
| 3b：污点外传 | `get_order` → `send_email` 外部域名 | 403 `[customer_pii_exfiltration]`；发内部域放行 |
| 4：跨 Agent | 同 task 下定价 -8%，营销发 20% 券 | 营销侧 403 `[price_and_coupon_stack]`，multiplicative 算出 -26.4% 击穿 -25 线；两步各自合规 |
| 5：一棵树一次批 | 5 步调价计划 `POST /v1/plans` | 终态 -40.9 越 deny 线 → high 待批；批准后逐步执行（`plan_id` + token 三重校验）；**篡改参数 → 哈希失配 403**；批准后状态变化 → 执行时重新求值拦截 |

一键复现：`make scenarios`。对应测试：`tests/test_gateway_api.py`（场景段）、
`tests/test_scenarios.py`、`tests/test_concurrency.py`。

**注意幂等语义**：完全相同的重复调用会被重放保护挡下（返回首次响应）——
「拆成 10 次各降 5%」逐字重复时实际是 1 次执行 + 9 次重放。连续降价必须
逐次微调幅度或换商品表达（spec §6.2 回写）。

## 几个可以现场验证的行为

```bash
# 1. 组合风险：拆成多次各降 5%，单次全合规，但预算阶梯在第 7 次扣下（202）、
#    批准执行后透支，连读都被拒——「单次策略不够」的直接证据

# 2. 幂等：同一调用发两次，第二次返回 replayed=true，商城只被改一次
#    599900 → 569905，而不是 539910

# 3. fail-closed：策略文件缺失 / 引用不存在的工具 → 网关拒绝启动

# 4. Provenance：新建会话直接改 p-iphone → 403（没先读过这个实体）

# 5. plan_token：批准计划后篡改参数（-3.0 改 -30，语法层拦不住的调包）
#    → 哈希失配 403，商城未被改动——「批准的是这一个计划，不是任意授权」
```

## 设计文档

- [系统设计](docs/superpowers/specs/01-session-risk-guardrail-design.md) —— 完整设计（含实测数字与实现要点）
- [同类方案调研](docs/research/01-agent-guardrail-competitive-analysis.md) —— 25+ 个开源项目的设计判断与差异主张
- [OWASP 合规映射](docs/owasp-mapping.md) —— Agentic Top 10 逐条覆盖矩阵
- [生产部署](deploy/README.md) —— 单节点部署、备份恢复、审计锚点与回滚

## 交付阶段

系统按五个阶段推进。每一阶段先界定问题，再交付可复现的证据：

1. **领域与基线**：[商城与工具层](docs/superpowers/plans/02-shop-and-tool-layer.md)、[标配能力层](docs/superpowers/plans/01-baseline-capability-layer.md)
2. **会话状态与组合风险**：[会话风险层](docs/superpowers/plans/03-session-risk-layer.md)
3. **计划审批与交付闭环**：[计划级审批](docs/superpowers/plans/04-plan-approval-layer.md)、[M5-M11 收尾](docs/superpowers/plans/05-m5-to-m11.md)
4. **调研复核与通用域验证**：[调研复核与通用域验证 实现计划](docs/superpowers/plans/06-research-hardening.md)
5. **单节点生产基线**：[生产交付阶段](docs/superpowers/plans/08-v1.2.0-production.md)

01-07 是分层实现计划：已完成步骤已经回勾，仍在正文中保留了当时的设计步骤和
代码快照。遇到与当前实现不一致的细节，以系统设计、`src/` 和 `tests/` 为准。

能力完备性和当前缺口见系统设计 §22。

## 已知局限

完整清单见 [docs/limitations.md](docs/limitations.md)。四个最要紧的：

1. **接入完整性不可验证**：Agent 若完全绕开网关直连商城，本系统毫无作用。
   这是所有同类项目（含本项目）的结构性问题。
2. **对抗语料全部自编**（`synthetic: true`）：拦截率数字的有效性以语料质量
   为上限；未接公开真实事故重放集。
3. **哈希链只证明「记录未被篡改」，不证明「记录内容在写入时为真」**；
   且尾部截断需要外部锚点才能发现；生产部署必须把 `audit-anchor` 输出送到
   另一套存储。
4. **生产基线是单节点**：SQLite 单写者和进程内事件总线不支持多副本共享。
   高可用扩展需要先替换会话与审计存储后端。

## 开发

```bash
make test    # 全部测试
make lint    # ruff
make clean   # 重建开发库（表结构变更后需要）
```

技术栈：Python 3.11+ / FastAPI / Pydantic v2 / Jinja2 / aiosqlite / httpx / PyYAML / jsonschema / pytest + hypothesis / uv / ruff
