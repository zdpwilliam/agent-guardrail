# 生产部署

本仓库提供的是**单节点生产基线**：一个网关副本、一个 SQLite 数据文件、TLS
由 Ingress 终止。它适合内部试点和单业务域上线，不把 SQLite 包装成高可用
数据库，也不支持多副本同时写入。

## 部署前提

- Kubernetes 1.29+，已安装 Ingress Controller，并有可用的 TLS Secret。
- StorageClass 支持 ReadWriteOnce，生产环境应启用卷加密。
- Secret 由部署系统注入，不写进 Git。
- 外部日志系统能保存 `audit-anchor` 输出的 JSONL。锚点不能只和数据库放在
  同一块盘上。

镜像中的策略文件与代码一起发布。策略调整走新镜像 tag；回滚时回滚镜像，
策略也会随版本一起回到上一个已验证状态。

## 首次部署

```bash
TAG=1.2.0
IMAGE=registry.example.com/agent-guardrail:${TAG}

docker build -t "$IMAGE" .
docker push "$IMAGE"

kubectl create namespace guardrail --dry-run=client -o yaml | kubectl apply -f -
kubectl -n guardrail create secret generic guardrail-console \
  --from-literal=console-key='<long-random-console-key>' \
  --from-literal=agent-api-keys='{"<agent-key>":"ops_agent"}' \
  --from-literal=approver-api-keys='{"<approver-key>":"security_lead"}'

# 将 deploy/k8s/guardrail.yaml 中的 image 和 Ingress host 改为你的值后执行
kubectl apply -f deploy/k8s/
```

生产环境还必须在 Deployment 中设置：

- `GUARDRAIL_ENV=production`
- `GUARDRAIL_CONSOLE_KEY`
- `GUARDRAIL_CONSOLE_APPROVER_ID`
- `GUARDRAIL_AGENT_API_KEYS`
- `GUARDRAIL_APPROVER_API_KEYS`
- `GATEWAY_DB_PATH=/data/gateway.db`
- `SHOP_DB_PATH=/data/shop.db`（仅商城进程）

缺失任一网关密钥，进程会拒绝启动。这是 fail-closed，而不是运行后再返回不
确定结果。

## 探针与日志

| 入口 | 用途 |
|---|---|
| `/healthz` | 进程存活，不查外部依赖 |
| `/readyz` | 检查网关数据库、策略和商城依赖 |
| `/metrics` | 决策计数与拒绝原因；启用鉴权后需要 API key |
| stdout | 生产环境输出逐行 JSON 访问日志 |

`/metrics` 在鉴权启用后也会校验 API key。Prometheus 抓取配置需要带
`X-API-Key` 或 Bearer token。

所有 HTTP 响应都会带 `X-Content-Type-Options`、`X-Frame-Options`、
`Referrer-Policy`、`Permissions-Policy` 和基础 CSP；生产环境额外发送 HSTS。

## 数据与备份

网关和商城各有一块 PVC。SQLite 已启用 WAL、`busy_timeout` 和
`synchronous=NORMAL`，但仍只有单写者语义。

审计脱敏从当前版本开始对新写入生效。旧数据库中已经存在的原始 `args` 不会
被在线重写，因为重写会破坏历史哈希链；升级前应评估是否保留旧库，或把旧库
归档后从空库开始新的审计链。

备份采用卷快照优先；逻辑复制时必须保证写入已停止，不能只复制主库文件而
忽略 `-wal` / `-shm`。恢复流程：

1. 缩容 gateway 与 shop 到 0。
2. 从卷快照或冷备份恢复对应 PVC。
3. 先启动旧版本，确认 `/readyz` 正常。
4. 再按发布计划升级镜像。

## 保留期与审计锚点

清理终态计划和已解决审批单：

```bash
python -m guardrail maintenance --db /data/gateway.db --retention-days 365
```

该命令不会删除审计链。审计链是追加日志，自动截断会破坏哈希连续性；需要
长期归档时，先把数据导出到 WORM/对象存储，再按合规要求决定是否重建链。

定期把链头写到外部系统：

```bash
python -m guardrail audit-anchor --db /data/gateway.db
```

把输出交给集中日志或对象存储，校验时：

```bash
python -m guardrail verify --db /data/gateway.db --anchor-file /anchors/guardrail.jsonl
```

没有外部锚点，只能发现现存前缀被改写，不能发现最后若干条被截断。

## 发布与回滚

1. 备份网关与商城卷。
2. 构建新镜像并固定 tag，不使用 `latest`。
3. 应用 Deployment；迁移只向前执行，记录 `schema_migrations`。
4. 验证 `/readyz`、`/metrics`、未认证请求返回 401、限流返回 429。
5. 若迁移失败，停止写入，回滚镜像并用备份恢复数据库。

策略版本随镜像发布；如果需要把策略放到 ConfigMap，必须使用不可变或带版本
后缀的 ConfigMap，并同步执行 `policy-lint`。不要热覆盖同一个策略文件。

## 明确不支持

- 多网关副本共享 SQLite；需要横向扩展时先替换外置存储。
- 跨区域高可用、自动故障转移和 HPA 水平扩容。
- 策略热加载、会话 kill switch、审批超时升级和 OTel 导出。

这些边界不是隐藏项；它们决定当前版本适合“单节点、受控入口、可备份恢复”
的生产试点，而不是多租户高可用平台。
