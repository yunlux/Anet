# Anet 演进路线

## 2026-07-24 定位与优先级重置

Anet 当前的产品窄腰是：

> **Agent Fabric 的身份层 + 可达性层 + 传输层 + 能力交换层。**

Anet 运行在现有以太网、Wi-Fi、蜂窝网络和操作系统之上。它不负责替代
Node B 的规划、Omnigent 的运行控制、Plano 的模型数据面、gbrain 的长期记忆，
也不把自己扩展成聊天软件、VPN、模型路由器或全局去中心化网络。

当前部署现实已调整：两台手机都已安装 Android Remote Control MCP 1.9，备用机
还保持无线调试，因此近期不再开发或部署额外 Android App。目标拓扑先让 WSL
Anet 与现成人机渠道形成可用闭环：

```text
Discord 社交 / 现有 Android Remote Control MCP
        ↕ 严格适配器边界
WSL Anet（身份、社会证据、标签、门限、可信度）
        ↕ 直连优先 / Relay + Mailbox 回退
已固定的 Agent peers
```

手机当前只是现有 Remote Control MCP 的人机/自动化端点，不创建 Anet Node ID，
不复制 WSL node home，也不成为高风险签名授权根。未来若现成 App 无法满足明确
需求，再恢复独立 Companion 评估。Anet 当前只处理 WSL 内的 Agent 身份、通信、
Discord 社交证据与可解释门限，不负责判断人的内在状态。

系统还必须区分四个正交通信平面：

- Agent Data Plane：任务、状态、结果、Artifact、证据和能力调用；
- Human Interaction Plane：对话、报告、提醒、解释、选择和授权；
- Control Plane：身份、可达性、路径、权限、会话、版本和撤销；
- Observation Plane：经过授权和端侧过滤的人类、设备、网络与环境事件。

它们是安全、数据生命周期和消费者边界，不等于 QUIC 的
`CONTROL/TASK/ARTIFACT/PULSE` 传输通道。

本轮优先级依据两条路线共同校准：

1. 近期产品路线以“稳定身份、永远先可通信、再优化路径”为主；
2. 高性能研究路线坚持控制面可审计、数据面可插拔，先证明任务收益，再购买或
   下沉到 RDMA、DPU、FPGA、CXL 和光互联。

任务排序依次看：是否解除目标拓扑的阻塞、是否能形成真实部署证据、是否保护
窄腰的长期兼容性、是否降低后续返工，以及单位成本能否带来可测量价值。

## 已有基础

v0.1–v0.11 已完成以下可复用能力：

- Node ID 由节点公钥派生，身份不等于 IP、端口或 Carrier；
- 签名 Peer Card、显式配对、固定信任、本地撤销和独立 `ANET_HOME`；
- TLS 直连、host/LAN/WAN 作用域 Locator、raw/SOCKS 拨号器、路径健康度、
  失败冷却、恢复滞回、候选竞速和有界跨 Carrier 复制；
- Directory 与 WebDAV 异步密文 Carrier、离线存储转发、逐阶段 ACK、回执、
  Packet 去重和 peer-scoped 一次性预密钥；
- stdio 字节流窄腰，可接入 SSH、串口、无线电 modem 或实验传输；
- 最小权限 MCP、durable consumer、typed task/status/result/cancel、执行幂等
  账本、sender/capability 策略和协作取消；
- A2A 1.0 的本地纯映射、持久任务聚合、事件游标、dispatch outbox 和取消
  fan-out。

这些成果证明了“身份和端到端密文可脱离固定 Carrier”，但尚未证明目标产品：

- 已有 Ahub/Rendezvous/Mailbox、运维入口、自适应 StoreCarrier 和经过真实
  独立进程验证的 bounded live byte Relay；显式节点 API 已承载现有 TLS/sync/
  receipt；持久 owner listener、peer-scoped discovery、NodeConfig 与
  AdaptiveRouter 自动接入也已完成，但尚未抽象为独立 `SessionCarrier`，也没有
  生产 TLS/rate-limit 配置和真实公网运行证据；
- Peer Card 仍混合长期身份描述和较动态的地址信息；NodeDescriptor 与短期
  ReachabilityRecord 已由 Ahub Carrier 发布，动态记录作为临时 overlay 接入
  direct/health/dialer probe 与 locator CLI，但不写入长期 PeerBook；
- 已有短期、可过期、防回放的 `ReachabilityRecord`，真实节点会在 Ahub
  StoreCarrier 同步时持续刷新；
- 没有 QUIC 会话、LAN 签名发现、NAT 穿透或移动网络迁移；
- Android Companion 协议和实验 App 已有实现，但当前部署由 Android Remote
  Control MCP 1.9 替代，真机 Anet Node/签名人工授权不再是近期阻塞项；
- Discord 账号已通过本地 HMAC 假名、社会证据账本与 Node ID/Peer trust 分开；
  尚未完成真实 Discord test Guild 部署和精度标定；
- 没有 Observation Plane 的端侧隐私过滤、批处理和数据等级；
- 没有跨物理设备、跨运营商和连续 7 天的目标拓扑验收；
- Artifact、State Delta 和 PULSE 仍未成为稳定的数据面通道；
- A2A 尚未通过官方 SDK 互操作，也没有 HTTP 网关。

## P0：先形成可用的人机闭环

P0 是当前唯一主线。P1 以前的任务不能被 A2A、群组、匿名路由、latent 或硬件
实验抢占。

### P0.1 身份描述与动态可达性分离

把稳定信任与短期地址变化拆开，同时保持现有 Node ID 和已固定 Peer Card 可迁移：

当前进度：纯 `NodeDescriptor v2`、`ReachabilityRecord v1`、
`HumanDeviceGrant/Revocation v1`、严格序列化、内存状态机和 SQLite 原子持久
checkpoint 已完成；`NodeDescriptor/ReachabilityRecord` 已接入 P0.2 Ahub
Rendezvous，Ahub Carrier 已在所属节点 home 保持公开 descriptor revision
checkpoint，并在真实 Anet 节点的 Ahub StoreCarrier 同步中发布短期
ReachabilityRecord，使用独立 `reachability-state.json` 跨重启延续序列；
运行中的节点会把经过 PeerCard 公钥钉扎校验的记录作为临时 locator overlay，
direct/health/dialer probe 会优先使用动态候选；`peer-reachability` CLI 已可查询
Ahub 当前记录，且不改写长期 PeerBook。

- 定义长期 `NodeDescriptor/PeerCard v2`：Node ID、身份键、静态协议能力、
  card sequence、前序摘要、有效期和轮换/撤销证据；
- 定义短期签名 `ReachabilityRecord v1`：Node ID、session ID、候选 Locator、
  observed candidate、relay reservation、capability digest、序号和到期时间；
- 地址更新使用受控命令和签名记录，不允许目录替节点改写；
- 旧 Peer Card 只进入显式兼容路径，不静默提升为新的动态记录；
- Windows、WSL、Android 始终使用独立 Node ID 和私有 home；可选 device group
  只表达归属，不共享密钥。
- 定义最小 `HumanPrincipal + HumanDeviceGrant`：稳定人类主体授权主力手机
  Node ID 持有有范围、有时限、可撤销的 `approval.sign` 能力；
- Discord、ChatGPT、Telegram 或系统账号只能成为交互适配器，不能替代
  HumanPrincipal、Node ID 或签名授权。

验收：

- Wi-Fi/IP/端口变化不改变 Node ID、不要求重新配对；
- 过期、回滚、重放、跨节点替换和 sequence 分叉全部失败关闭；
- v1 已部署节点可以滚动升级，现有 trust pin 不丢失；
- Locator 变化只能通过签名 API/CLI，不能靠手改 `config.json` 或 `card.json`。
- 手机丢失时可只撤销设备授权，不更换 HumanPrincipal；换机后可授权新 Node ID；
- 高风险授权不能由普通聊天文字、Discord 按钮或服务端账号冒充。

### P0.2 公网锚点 MVP

先实现一个可部署、可观测、内容不可见的锚点服务，不先拆五个微服务：

当前进度：无私钥 `AhubService`、独立 SQLite allowlist/nonce/mailbox、
Rendezvous、签名 HTTP 请求、密文 custody、领取租约、显式 settle、配额和薄
ASGI/HTTPS 客户端已完成；operator allow/disallow/status/purge/checkpoint、
loopback-safe Uvicorn 入口、健康检查、脱敏日志和离线备份 runbook 已补齐，并由
真实独立 HTTP 进程的终止/重启/领取测试验证。持久 peer-scoped reservation、
双方签名 WebSocket upgrade、双向 opaque binary forwarding、backpressure 及
frame/byte/duration/node 配额也已完成；真实进程覆盖 Ahub 重启、越权、nonce
重放、双向字节、字节/时长关闭和日志脱敏。显式 one-shot 节点 API 已通过临时
unadvertised loopback bridge 运行现有 TLS 1.3、签名身份、sync 和 receipt，
并由两个关闭常驻 listener/direct 的 disposable 节点验证单 Inbox 与双方 pending
收敛。配置启用 live Relay 后，owner 会按明确 peer 持续刷新 reservation 并以
抖动退避恢复；对端只可发现授权给自己的 reservation，AdaptiveRouter 会先尝试
节点间 TLS Relay，再保留 Mailbox StoreCarrier 作为离线回退。真实进程已覆盖
Ahub 重启后的自动重建与第二次业务收敛。当前仍是一个复合 Ahub 路径，尚未
完成 P1 的 `SessionCarrier`/`StoreCarrier` 窄腰，也没有生产级 rate limiter、
正式服务包和真实公网/手机验证。

2026-08-06 真实公网锚点验证已通过：`ahub-serve` 部署于 WSL 的
`~/.local/state/anet/ahub-public`（loopback:8422，systemd user service
`anet-ahub-public`），经 Cloudflare Tunnel 命名隧道暴露为
`https://ahub.905527.xyz`（cloudflared 自身即"无公网入站端口"的真实场景）。
公网 `/healthz` 返回 HTTP 200。两个 disposable WSL 测试节点（discord-test 与
ahub-peer-b）经公网域名完成 Rendezvous（descriptors=2）、Live Relay
（relay_reservations=1）、密文跨公网交付（inbox 收到
"hello via public ahub"）与 settle 证明（retained_settlements=2）。日志仅含
聚合 route 类，无 Node ID/正文。真实手机/mac 设备验证因 Android 端暂停、
改用 Android Remote Control MCP 而推迟。详见
[`docs/AHUB_V1.md`](docs/AHUB_V1.md) 与
[`docs/AHUB_OPERATIONS.md`](docs/AHUB_OPERATIONS.md)，Relay 精确边界见
[`docs/RELAY_V1.md`](docs/RELAY_V1.md)。

自适应 `AhubStoreCarrier` 现已接入 NodeConfig、CLI、AdaptiveRouter 与
PacketStore：上传只标记 path custody；目标本地持久化后签名
`DestinationSettlement v1`，发送端以 pinned key + 本地 raw digest 验证后才
标记目的 ACK。真实双节点/独立 Ahub 进程覆盖锚点重启、业务 receipt、双方
pending 收敛和单 Inbox。当前只支持 depth-zero 直达最终目标，不冒充 live Relay。

- Rendezvous：接收和查询短期 `ReachabilityRecord`；
- Relay：双方已能经各自主动出站 WebSocket 自动取得有界 byte stream，并在该流
  内终止节点间 TLS、运行现有签名身份/sync 协议；P0 下一项是生产化与真实公网/
  手机验证，Relay→直连 make-before-break 属于 P1；
- Mailbox：节点离线时保存有 TTL、配额和去重键的不可变密文 Envelope；
- 节点全部主动出站连接；服务端不持有节点长期私钥；
- 区分 relay custody ACK、目的节点 ACK、端到端 receipt 和本地 consumer ACK；
- 服务端具备限额、退避、滥用隔离、最小日志和可删除的短期状态。

第一版暂不实现 Registry 和 Artifact 服务；能力摘要放在短期记录中，大对象继续
使用现有 Packet/Carrier 上限内的测试载荷。

验收：

- 主电脑节点与主力手机在无公网入站端口时可立即经 Relay 通信；
- 一端离线、锚点重启、客户端重启后密文仍可收敛；
- 锚点数据库和日志不出现任务正文、私钥或可伪造节点身份的材料；
- 重试不会产生第二条 Inbox 消息或第二次任务副作用；
- Relay 不会把 custody 成功误报为目的节点完成。

### P0.3 WSL Discord Social Discovery 最小闭环

当前主线不再是 Android App，而是在 WSL 已有 Anet runtime 内建立第一条真实
Agent 社交与发现闭环：

```text
Discord allowlisted channel
  → metadata / explicit bot mention
  → pseudonymous local evidence ledger
  → labels + score + confidence + threshold
  → social.discord.signal
  → already trusted Anet Agent
  → threshold-gated explicit reply
```

当前进度：`src/anet/social.py` 已实现 exact-field `social.discord.signal`、
证据加权、信誉分、置信度、单调门限和标签命名空间；`discord_social.py` 已实现
REST v10 allowlist polling、Guild/Channel 绑定、本地 HMAC 假名、SQLite
事件/actor/label/cursor/reply 账本、`Retry-After`、安全回复与 Anet durable
queue 接入。Bridge 运行在已有 `anet serve` 中，不启动第二个使用同一 node home
的进程。完整边界见
[`docs/DISCORD_SOCIAL_V1.md`](docs/DISCORD_SOCIAL_V1.md)。

门限动作依次为：

- `observe`：允许记录最小元数据，不代表认可；
- `surface`：可投给明确 trusted Agent 复核；
- `reply`：仅明确提及 Bot、非 bot/webhook，且分数与置信度达标；
- `amplify`：可作为后续 feed 候选；
- `connect_candidate`：只建议核验，不导入 Peer Card、不授予 capability。

安全与隐私规则：

- 默认 `mentions` 模式只保留明确提及 Bot 的 2,000 字符以内正文；其余消息只保留
  元数据，附件只记 `content:attachment`，不复制文件名、URL 或内容；
- Discord user/Guild/Channel ID 只留在源 WSL 私有账本；Anet signal 使用本地
  HMAC 假名；
- bot token 只从环境变量读取；不写配置、SQLite、日志或 CLI JSON；
- `risk:block/spam/impersonation/malware` 无条件降为 `observe`；
- 自动互动证据有上限，不能靠刷 mention/reaction 无限涨分；
- operator label 和 adapter label 分 namespace，外部事件不能伪造
  `relationship:vouched`；
- 回复禁用所有 mention 解析、绑定原消息并使用 Discord `enforce_nonce`；
- 任何分数、role、标签或 `connect_candidate` 都不能建立 Anet trust。

近期验收：

2026-08-06 主体闭环已在真实 Discord 环境验证通过：WSL 独立测试节点
（`~/.local/share/anet/nodes/discord-test`，systemd `anet-discord-test`）配置
专用 test Guild/channel（`content-mode mentions`），bot token 仅存
`~/.config/anet/discord-social.env`（600）并经 `ANET_DISCORD_BOT_TOKEN`
注入，未写入配置/SQLite/日志。REST v10 polling 正常（`ingested` 随新消息
递增）。非 mention 消息按 `metadata` 保存且正文为空；显式 bot 用户 mention
（`@Guwen`）按 `mention` 保存 ≤2000 字符正文，标签含 `interaction:mention`，
证据加权出现 `bounded mentions +2`（分数 50→51、置信度 5→8），门限判定
`surface`；未达 reply 门限时 `discord-social-reply` fail-closed 拒绝。bot
自消息被过滤（`discord_social.py:734`）。角色 mention（`<@&...>`）不进入
Discord `mentions[]` 数组，v1 按 metadata 处理，属已知语义边界。仍待验证：
达标 reply 实际发送、429/权限撤销恢复、WSL 重启恢复与七天连续性。

1. 用专用 test Guild、最小权限 Bot 和非生产 token 在 WSL Node A 上运行；
2. 验证 mention content、metadata-only、标签、分数、置信度和门限输出；
3. 验证 WSL/服务重启、token 轮换、429、频道权限撤销和 Anet 目标离线；
4. 测量 surface precision、人工回复率、误拦截率和重复 signal 数；
5. 达到可用精度后再引入 EigenFlux 式 profile/subscribe matcher；matcher 仍不
   成为授权者。

### 暂停项：Android Companion

`anet.companion v1`、Python/Kotlin 协议门禁、Node B approval execution gate 和
实验 Android App 已保留。当前两台手机由 Android Remote Control MCP 1.9
承担交互，备用机保持无线调试，所以不继续开发通知 UI、前台服务、手机 Anet
身份或 Keystore 真机闭环。只有现成 App 出现无法绕过的明确能力缺口时才恢复。

### P0.4 真实部署与连续性门禁

部署顺序：

1. 当前 WSL 独立节点和专用 Discord test Guild；
2. Discord social ledger → trusted Anet Agent → explicit reply；
3. 一台公网 Ahub 及 Relay/Mailbox 回退；
4. 第二台物理 Agent 设备；
5. Android Remote Control MCP 继续作为手机交互层，不创建额外 Anet App。

连续 7 天记录：

- 端到端可达率、任务成功率、p50/p95/p99 首包与完成延迟；
- Relay 占比、直连占比、路径切换时间和离线队列停留时间；
- 重复 Packet 数、重复副作用数、拒绝/重放数和人工介入次数；
- Discord surface precision、回复率、误拦截率、score/confidence 分布和人工
  标签变更次数；
- metadata/mention 投影比例、正文与外部 ID 泄漏检查、429 和权限失败恢复时间；
- social signal 去重率、Anet queue 停留时间和 Discord reply 幂等结果；
- 每任务字节数、服务端存储量和锚点运营成本；
- 服务端可见元数据清单。

门禁为：随机断网、重启和地址变化后无消息丢失、无重复副作用，并能证明每条
消息实际经哪条路径抵达。当前 Windows/WSL 同机验证不计作此门禁。

## P1：从“能通信”升级为路径自适应

P1 必须建立在可用 Relay 上，遵循 `RELAY_READY → PROBING → DIRECT_READY`，
不能等待直连成功才开始传输。

### P1.1 Carrier 与 Session 窄腰

不强迫同步会话和异步邮箱共用一个不合适的接口，明确两类能力：

- `SessionCarrier`：可靠流、可选 Datagram、迁移、关闭和实时指标；
- `StoreCarrier`：发布不可变密文、扫描、保管 ACK、消费和隔离；
- 上层统一处理 Node ID、Packet/Frame、幂等、策略和路径选择；
- 当前 TLS、stdio、Directory、WebDAV 作为兼容实现接入。

### P1.2 QUIC 默认会话与四类通道

- QUIC Stream 承载 `CONTROL`、`TASK`、`ARTIFACT`；
- QUIC DATAGRAM 承载只保留最新值的 `PULSE`；
- 逻辑 task/context/session ID 不绑定 QUIC connection ID；
- UDP 不可用时回退 TLS/Relay，不把 QUIC 变成单点依赖。

### P1.3 LAN 发现与直连升级

- 实现小型签名 Beacon，mDNS/multicast 只作为发现 Carrier；
- 验证 Node ID、签名、授权和短期 epoch 后才探测；
- 并行评估 host IPC、LAN IPv6、LAN IPv4 和 Relay；
- Relay 上的业务在直连完成认证后 make-before-break 升级。

### P1.4 WAN 穿透与主力手机移动连续性

- 候选地址交换、observed address、NAT 类型探测和同步打洞；
- 打洞失败永久允许 Relay 回退，不把成功率当作正确性前提；
- Companion 采用前台服务或系统允许的长连接机制，只主动出站；
- 验证 `Wi-Fi → 5G → 新 Wi-Fi` 时逻辑任务和会话不丢失。
- 参考 Matrix `/sync` 收敛统一 `SyncCursor v1`：cursor 绑定 peer、filter 和
  log epoch；有限批次显式返回 `limited` 与 gap/backfill token；游标过旧时
  fail closed 并要求快照 + delta 重建，不能把 cursor 当身份或授权。

## P2：扩展 Agent、Human 与 Observation 平面

### P2.1 Agent Data Plane

1. `ARTIFACT`：BLAKE3/CID、分块、断点续传、内容清单和临时缓存；
2. `capability.query/invoke`：版本、schema、约束、负载和授权分离；
3. `state.patch/event.append/receipt.commit`：增量状态和语义确认；
4. 让 `CONTROL/TASK/ARTIFACT/PULSE` 的 QoS、保留、重放和 ACK 语义显式化。

### P2.2 Human Interaction Plane

- 定义 `Alert/Report/Choice/ApprovalRequest/ApprovalDecision/UserResponse`；
- 在 P0.3 Discord Social Bridge 上扩展 slash command、结构化 Choice 和报告；
  Telegram 只在有明确新增价值后评估；
- ChatGPT/Codex 是人类主动使用的顾问、执行入口或复核者，不作为可靠 Anet 节点；
- 高风险操作只能经 Companion 签名；聊天渠道的按钮和回复最多产生待确认意图；
- 支持紧急通知、普通消息、复杂报告、每日汇总和静默信息的明确路由策略。

### P2.3 Observation Plane 与 Humon 边界扩展

P0.3 只证明低风险数据和 Self Report 的端侧最小化闭环。本阶段才逐权限扩展
Phone/Body/Environment/Social Sense；Anet 仍只负责传递，不负责推断人的内在
状态。主力手机保持：

```text
Event Collector → Privacy Filter → Feature Extractor
→ Local Episode Builder → Immediate Rule Engine → Anet Client
```

- 心跳/电量/网络只保留最新，可丢失；
- 高频屏幕和应用事件端侧聚合、批量压缩、允许延迟；
- 状态变化可靠低延迟，紧急安全事件要求端到端确认；
- 原始音频、图片、精确轨迹和聊天正文默认不上传；
- 每日摘要可延迟持久化，用户授权结果必须签名、可靠、不可重放；
- Discord 上下文按 L0 元数据、L1 类别、L2 授权摘要、L3 明确授权正文分级；
- Self Report 保留来源和用户显式性，由 Humon 决定如何加权。
- Humon 可输出多维、带置信度的 `HumanState` 给 Node B，但这是 Humon 的领域对象，
  不是 Anet 从单一传感事件生成的标签；Anet 只保证其来源、版本、TTL 和投递语义。

备用手机在本阶段作为独立 Node ID 接入，声明
`humon.environment_sensor/android.automation/network.probe/human.fallback_interface`
等能力；主力手机声明
`humon.primary_sensor/human.primary_interface/human.approval_signer`。故障接管
按能力和授权选择，不按“手机名称”硬编码。

### P2.4 Runtime 与生态适配

1. Omnigent adapter：把外部副作用 idempotency key 贯穿到执行端；
2. Humon adapter：只接收经过同意、最小化和标注来源的 Observation/Episode；
3. Amesh Discord bridge：把显式 allowlist 事件升级为独立 profile/subscribe
   discovery 与 agent grant 中间层；ChatGPT 仍只做出口或人工接力；
4. 能力目录：发布版本化 schema、负载和可用性，不发布敏感传感数据；
5. 参考 EigenFlux 扩展 Amesh Signal Discovery Plane：声明式 profile/subscribe、
   public-safe SignalEnvelope、可解释匹配、语义去重和反馈；匹配器不能成为
   agent grant、平台权限或其他项目的信任根。当前独立切片见
   `docs/AMESH_STANDALONE_ARCHITECTURE.md`。

### P2.5 A2A 边缘互操作

固定官方 SDK 版本完成严格互操作测试，再开放 HTTP/streaming 网关。

A2A 是边缘生态协议，不替代 Anet 的身份、信任、Carrier 或 durable task 窄腰。
现有 A2A outbox 和取消实现保留，但官方 SDK 互操作从“立即下一项”调整为 P2.5。

验收不以字段数量为准，而以跨 runtime 的任务完成率、重复副作用为零、Artifact
恢复能力、schema 兼容和最小权限证据为准。

EigenFlux 与 Matrix 的采纳、改造和暂不引入项见
[`docs/EIGENFLUX_MATRIX_REFERENCE.md`](docs/EIGENFLUX_MATRIX_REFERENCE.md)。

### P2.6 Abazr（ABA）上层实验

Abazr 是独立于 Anet/Ahub 的 Agent Bazaar 产品。它复用 Signal、typed task、
Artifact、Evidence 和明确 capability policy，提供非金融化的
Need/Offer/Match/Proposal/Agreement/Fulfillment 协作闭环；Matcher 不成为信任根，
Ahub 不承担市场、评分或结算。ABA 核心保持链无关，Web3 只通过可选索引、
Attestation、内容寻址和 Settlement Adapter 接入。

当前只允许 ABA-D0 本地纵向 Demo，用于固定领域语言和失败语义，不抢占 P0
真实部署主线。完整蓝图、阶段门禁与 Demo 边界见
[`docs/ABAZR_BLUEPRINT.md`](docs/ABAZR_BLUEPRINT.md)。

## P3：身份与会话安全成熟化

Signal/Sesame 只作为经过部署验证的设计参考，不复制其账号体系或 AGPL 源码。
P0 已提供最小 HumanDeviceGrant；本阶段补齐完整生命周期：

1. 人类/Agent 根主体到设备 Node ID 的有序授权、轮换、过期和可传播撤销；
2. Peer Card rotation history、本地 transparency log 和 gossip consistency；
3. 有界 per-device session state、乱序窗口和 crash-safe 事务模型；
4. 采用有审计实现的标准 ratchet 实验，与当前 OPK 模式并行；
5. relay 短期 delivery capability，使滥用控制不直接暴露长期 sender；
6. 有真实多 Agent 组需求后再评估 MLS；
7. PQXDH/混合 PQ 只作为版本化迁移目标，不提前宣称量子安全。

系统密钥库、明文保留边界和可验证销毁在本阶段进入发布门禁。能解密始终不等于
有能力调用工具；会话密码状态不能替代 capability policy。

## P4：机器高效表示与硬件数据面

这是一条独立实验轨，不改变默认生产路径。默认退路始终是：

```text
Typed IR + State Delta + ArtifactRef + 可观测 QUIC/TLS
```

### P4.0 软件基线

- 建立统一 benchmark：链路、协议、表示和真实任务四类指标；
- 对比全文、Typed IR、State Delta、ArtifactRef；
- 加入 netem 的延迟、丢包、乱序、断连和路径迁移矩阵；
- 记录 p50/p95/p99、CPU、内存复制、字节/token、首个有效结果时间、
  最终任务成功率、silent corruption 和 fallback 次数；
- 定义签名 Manifest：模型、tokenizer、codec、schema、digest、epoch 和策略。

### P4.1 两节点高速数据面

只有 P4.0 证明任务收益后，才投入 100/200GbE RoCE 或 InfiniBand：

- 对比 QUIC/TLS、AF_XDP 和 RDMA；
- 对比 IR、Delta、KV/量化 KV；
- 验证迁移、重启、重放、manifest 篡改和幂等恢复；
- KV/latent 只允许在同模型家族、受控可信边界和显式 capability 下实验。

### P4.2–P4.3 硬件下沉

- P4.2：DPU/SmartNIC/FPGA 的加密、队列、Manifest 验证和过滤下沉；
- P4.3：CXL pod、光互联和规模化路径评分。

升级条件是任务成功率不下降，同时总时延、CPU、字节或 token 成本存在显著、可
复现优势。未达标就停止在纯应用层，不用更昂贵硬件放大未验证架构。

## 暂停项

以下内容在对应门禁完成前不进入主线：

- 全球 DHT、全节点 Mesh 和去中心化共识；
- 公网匿名 Fabric、流量隐写或以规避审计为目标的混淆；
- 自创密码算法、自创一对一或群组 ratchet；
- 手机承担中继节点；
- 原始以太网广域路由；
- 未绑定模型/codec 指纹的 latent/KV；
- 未验证 latent/KV 直接触发高风险工具或写入长期知识库；
- 多租户或不可信边界共享明文 KV-cache；
- FPGA、CXL、硅光或专有总线采购。

## 紧接着执行的三个任务

1. 在专用 Discord test Guild 用最小权限 Bot 部署 P0.3：固定 allowlist、token
   环境文件和一个已 trusted 的 Anet destination，验证 mention → pseudonymous
   signal → Agent review → threshold-gated reply；
2. 连续采集 surface precision、reply rate、误拦截、score/confidence 分布、
   label 变化、重复 signal 和 429/权限/重启恢复证据，再据实调整门限；不根据
   单次成功放宽策略；
3. 收口 P0.2 Ahub 生产门禁并让 Discord social signal 在直连失败时经
   Relay/Mailbox 收敛；随后才实现 EigenFlux 式 profile/subscribe matcher 和
   Matrix 式统一 SyncCursor。

这三项完成前，A2A 官方 SDK 互操作、HTTP 网关、MLS、latent 和硬件数据面只保留
已有代码与研究记录，不继续扩面；Android Companion 同样保持暂停。
