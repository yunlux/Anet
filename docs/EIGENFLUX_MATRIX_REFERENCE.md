# EigenFlux 与 Matrix 对 Anet 的架构启发

本文把 EigenFlux 和 Matrix 当作两种互补参考，不把 Anet 改造成公共内容平台、
聊天系统或 Matrix homeserver：

- EigenFlux 主要回答“哪个 Agent 可能关心这个公开信号”；
- Matrix 主要回答“多设备、多服务端怎样同步事件历史和派生状态”；
- Anet 继续回答“已固定身份之间怎样以可恢复、可审计、Carrier 无关的方式传递
  typed task、事件、Artifact、控制对象和最小化 Observation”。

研究依据均为项目官方资料：

- [EigenFlux 官方仓库](https://github.com/phronesis-io/eigenflux)
- [Matrix 规范架构](https://spec.matrix.org/latest/)
- [Matrix Client-Server `/sync`](https://spec.matrix.org/latest/client-server-api/#get_matrixclientv3sync)
- [Matrix Rust SDK](https://github.com/matrix-org/matrix-rust-sdk)

## EigenFlux：采用信号发现，不采用中心授权

EigenFlux 的核心模型是 Agent 同时作为广播者和监听者，发布发现、需求或能力，
Hub 通过 profile、治理和相关性匹配推送个性化 feed。其实现还公开了异步 LLM
富化、向量检索、去重、反馈和声誉等内容分发机制。

Anet 值得采用：

1. **声明式订阅**：Agent 用版本化 profile 表达兴趣、能力、语言、负载和有效期，
   而不是为每一对 Agent 手写静态路由。
2. **高信噪比 signal envelope**：只发布摘要、schema、provenance、TTL、
   visibility 和获取后续详情所需的 capability reference。
3. **发现与传输分离**：匹配器只产出候选订阅者；实际内容仍经 Anet 的固定身份、
   capability policy、Packet/Artifact 和 durable consumer 传递。
4. **治理结果可解释**：匹配应返回规则版本、匹配原因和分数分解，不能只给一个
   不可审计的模型分数。
5. **反馈与去重**：相同 signal 的稳定语义 ID、来源质量和显式反馈可以降低多个
   Agent 重复搜索、重复总结和重复广播的成本。

需要改造：

- EigenFlux 当前 Hub/profile/feed 模型适合公开或团队共享信号；Anet 必须增加
  `visibility`、tenant、接收 capability 和敏感度门禁。
- “自然语言兴趣”只能帮助召回候选，不能成为授权规则。最终投递和执行仍使用
  精确 schema、sender、resource、预算与 capability。
- LLM enrichment、embedding、reputation 都应是可替换的网关模块；失败时 Anet
  的已知 peer、明确目标和 durable task 仍须工作。
- 广播必须经过结构化隐私降级。Companion Observation/Episode、Self Report、
  Approval、聊天正文、内部 URL、凭据和精确人类/设备元数据默认禁止进入发现面。

明确不采用：

- 不把公共 EigenFlux Hub 设为 Anet 的信任根、必经控制面或唯一 Registry；
- 不让匹配分数授予工具能力或高风险副作用权限；
- 不把全部 Agent profile、邻接关系和任务正文集中上传；
- 不直接复用代码，直至完成其“Apache 2.0 加附加条件”许可证与依赖边界审查。

## Matrix：采用同步状态机，不先采用完整房间联邦

Matrix 把通信表示为带类型的事件；规范明确要求应用把 event body 当成不可信数据
并自行做 schema 校验。它将一次性活动的 message events 与持久信息更新的 state
events 分开，并在 `/sync` 中用 `next_batch`/`since` 游标、long-poll、有限
timeline、gap 与 backfill 支持客户端断线恢复。

Anet 值得采用：

1. **不可变事件与派生状态分离**：Packet、task status、ApprovalDecision 是
   append-only 事实；当前任务状态、授权状态和设备授权是可重建的 materialized
   state。不得通过覆盖旧事件伪造历史。
2. **稳定 ID 与人类标签分离**：Node ID、task ID、subscription ID 和未来 group
   ID 是安全边界；label/alias 只用于显示和发现，解析后不能悄悄改变目标。
3. **恢复游标**：统一 `SyncCursor` 语义，绑定主体、过滤器和日志 epoch；增量结果
   显式返回 `next_cursor`、`limited` 和 gap/backfill token。
4. **主体与设备分离**：Matrix 的 user/device 区分和逐设备撤销印证 Anet 的
   HumanPrincipal/AgentPrincipal → 独立 Device Node ID 模型，但 Anet 不复制
   Matrix 的 homeserver 账号或设备密钥分发方式。
5. **严格类型命名与不可信解析**：继续保持 `agent.*`、`companion.*`、`control.*`
   的版本化 exact-field validator。能同步到本地不等于能进入可信 Inbox，更不
   等于能执行。
6. **协议核心与 UI/网络解耦**：Matrix Rust SDK 把无网络 I/O 的 crypto state
   machine 与中层 SDK、UI 层分开，并提供 Kotlin/Swift 等绑定。Android Companion
   也应把纯协议/密码/队列状态机与 Android service、通知 UI、网络适配器分层。

当前 Anet 已经具有相同方向的基础：

- Inbox consumer group 的持久起点、claim/ACK/NACK 和租约恢复；
- A2A append-only event sequence 与跨重启 cursor；
- Companion exact-field 收发门禁；
- Approval request/decision 事件、派生 authorization state 和 effect ledger；
- HumanPrincipal 与 Device Node ID 分离、签名 grant 和终局 revocation。

明确暂不采用：

- P0/P1 不实现 room DAG、state resolution、homeserver 全网联邦或完整历史复制；
- 不把 task/approval/observation 强行塞进聊天 room；
- 不采用 `@user:server` 作为 Anet 根身份，也不让域名或 homeserver 控制 Node ID；
- 不默认向所有 group 成员复制完整历史；Agent task 常常要求最小披露和定向授权；
- 没有真实多写者共享状态冲突前，不支付 Matrix room version 与 state resolution
  的复杂度成本。

## 对路线图的具体影响

### P0：在 WSL 完成 Discord 社交发现最小闭环

- 当前两台手机由 Android Remote Control MCP 1.9 承担交互，Companion 协议与
  实验 App 保留但暂停继续开发。
- 第一条 EigenFlux-inspired slice 是显式 allowlist 的 Discord Social Bridge：
  不可变 Discord event、HMAC 假名 actor、派生证据/人工标签、可解释 score 与
  独立 confidence、逐级动作门限。
- score、标签和 Discord 身份都只能产出发现与互动候选，不能创建 Anet Peer
  trust、capability 或高风险授权。投递仍只面向已固定的 Anet Peer。
- Discord cursor 只有在事件持久化且需要的 Anet queue 成功后才推进；事件 ID
  稳定、重复拉取不重复累计证据，保持 Matrix 式 event/state 分离与断线恢复。

### P1：把断线恢复提升为统一同步契约

- 为 Relay/Mailbox/Companion 定义绑定 peer、filter、epoch 的 opaque
  `SyncCursor v1`；
- 有限批次必须返回 `limited`，不能把截断误报为完整历史；
- 提供有界 backfill 和快照 + delta 恢复，游标过旧时显式失效并要求安全重建；
- cursor 是进度证明，不是身份、授权或可转移 bearer capability。

### P2：把直接 Discord adapter 泛化为可选 Signal Discovery Plane

定义 `signal.publish/profile.subscribe/signal.feedback` 网关：

```text
typed local event
  -> privacy classifier / public-safe projection
  -> signed SignalEnvelope
  -> optional matcher(s)
  -> candidate subscribers
  -> Anet capability check
  -> directed Packet or ArtifactRef
```

Discord P0 先用直接 Guild/Channel allowlist 收集真实精度数据。泛化后的首版只
允许 `public` 和明确 tenant-shared 的 Agent 知识、需求与能力摘要。所有 matcher
均可替换、可并行、可绕过；匹配结果不改变 Anet 信任图。

同阶段把 `event.append` 与 `state.patch` 的语义固定为：

- event 有稳定 ID、类型、sender、schema version、时间、TTL 和 provenance；
- state 由明确 `(namespace, object_id, state_key)` 唯一定位；
- reducer/version 决定如何从事件重建状态；
- 多写者冲突先用领域专用规则，只有确有共享房间式历史时才评估 DAG/state
  resolution。

## 结论

EigenFlux 可让 Anet 从“已知目标间可靠传递”扩展到“低成本发现值得连接的 Agent
和信号”；Matrix 可让 Anet 的事件、状态和断线同步从若干局部实现收敛为一个可
验证契约。两者都应位于 Anet 窄腰的上下层：前者不成为授权中心，后者不把窄腰
膨胀成聊天联邦。
