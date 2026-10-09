# 调研复核与通用域验证 实现计划

> **交付状态：两阶段均已实现并合入 v1.2.0。**
> **同步口径：本文件保留调研加固和第二领域验证的分阶段设计；当前实现以
> `src/guardrail/evaluation.py`、`src/guardrail/domains/corp.py`、corp 策略和
> 对应语料测试为准。**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## 范围

阶段一 解决“可信度如何被验证”，阶段二 解决“同一引擎能否进入第二个领域”。
两项工作合为一条推进线，但保留清晰的验收边界：

- 阶段一不改变核心引擎，只补语料、映射文档和确定性测试。
- 阶段二不复制引擎，只增加 corp 域工具、策略和外部来源语料。
- 两阶段都以 `make test`、`make lint` 和对应评测命令作为完成门槛。

## 背景

同类方案调研显示，护栏项目的可信度通常卡在三处：语料没有威胁框架编号，
策略效果缺少覆盖边界，评测只在本领域自证。阶段一 先补齐前两项，阶段二
再把 file/email 域接进同一套判定链，验证“领域是显式可控成本”这一设计。

---

## 阶段一：调研驱动加固

### A1. 对抗语料加威胁框架编号

- [x] **红测试**：`tests/test_evaluation.py` 要求 adversarial case 含
      `threat_refs`，格式限定为 `OWASP:T1..T10` 或 `ATLAS:AML.Txxxx`；
      新增 `owasp_coverage(cases)` 统计各条目覆盖数。
- [x] **实现**：给 `eval/cases_adversarial.yaml` 的 20 条 case 补齐
      `threat_refs`；`load_cases` 做格式校验；`make eval` 输出覆盖统计。
- [x] **验收**：红测转绿，全量测试和 lint 通过。

### A2. OWASP Agentic Top 10 映射

- [x] 写 `docs/owasp-mapping.md`：逐条给出本项目覆盖机制、状态
      （Full / Partial）和边界注记。
- [x] 预期覆盖：T2/T3/T4/T8/T9 为 Full，T1/T5/T6/T7/T10 为 Partial。
      不为了凑数把边界模糊成“已覆盖”。
- [x] README 和 `docs/limitations.md` 增加映射表入口。

### A3. 求值可复现性测试

- [x] **红测试**：`tests/test_determinism.py` 用 hypothesis 生成动作序列，
      在两个独立栈上执行，断言逐动作 `(status, decision)` 完全一致，
      两边的审计链都能独立校验通过。
- [x] **实现**：若出现非确定性，先定位根因再修；不把时序波动当成测试噪声。
- [x] **验收**：property test 进入 `make test`。

### A4. 交叉验证与暂缓项

- [x] 记录 OpenAPPA Bench-Corp 的许可、语料格式和接入成本。
- [x] 结论写入 `docs/limitations.md`：不直接跑其 bench runner，先对标
      评测方法；第二领域验证放到阶段二。
- [x] Claude Code hook 接入暂缓，等真实接入需求出现再评估。

### 阶段一完成标志

- [x] 对抗语料全部带威胁框架编号，评测能按条目聚合。
- [x] OWASP 映射逐条给出 Full / Partial 判断。
- [x] 双栈确定性 property test 通过。
- [x] 交叉验证结论和暂缓项都已写进文档。

---

## 阶段二：第二领域验证

### B1. corp 域工具

- [x] **红测试**：先注册 `list_files` / `read_file` / `write_file` / `send_email`
      四个工具；后续 07 阶段扩展到 `read_forum` / `post_forum` /
      `create_ticket` / `execute_wire`，最终 corp 域共 7 个工具并通过
      `assert_specs_valid`。
- [x] **实现**：`src/guardrail/domains/corp.py` 提供 `CorpState`，
      handler 在内存文件系统上读写，邮件记录外发地址。
- [x] **边界**：corp 工具与电商工具共用注册表，隔离靠策略权限表，
      不另造一套判定引擎。

### B2. corp 单次与组合策略

- [x] **红测试**：两份 corp 策略可加载，权限表只授权 `corp_agent`。
- [x] **实现**：`policies/corp_single_call.yaml` 负责单次上限与权限；
      `policies/corp_combined_risk.yaml` 负责污点外传和累计外发。
- [x] **验收**：`policy_lint` 覆盖 corp 策略，电商策略仍保持域隔离。

### B3. Bench-Corp 来源语料与评测

- [x] **红测试**：`load_cases("cases_corp.yaml")` 接受 `sourced: bench-corp`；
      语料包含 6 条对抗和 4 条正常场景。
- [x] **实现**：新增 `eval/cases_corp.yaml`；runner 支持独立的
      `single_policy_path`；CLI 增加 `eval-corp`。
- [x] **验收**：`make eval-corp` 输出三方表，full 模式拦截对抗、
      放行正常，single 模式保留组合风险漏拦的对照结果。

### B4. 文档收口

- [x] 更新 spec §21 和 `docs/limitations.md` 的 B4 状态。
- [x] README 记录第二领域结果和复现命令。
- [x] `make lint`、`make test`、`make eval`、`make eval-corp` 全部通过。

### 阶段二完成标志

- [x] 同一引擎、两份领域策略、两套语料各自跑出三方表。
- [x] corp 域污点外传与累计外发在 full 模式被拦，single 模式漏拦。
