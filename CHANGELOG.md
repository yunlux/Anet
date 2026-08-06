# Anet Changelog

## Unreleased

- 签名根控制页现在可通过 `control_publishers` 委派只用于被点名嵌套来源的社区
  发布者。委派 key 不写入本机 `trusted_keys`、不能签根页面或继续委派，也不改变
  PeerBook、节点授权、关系或信誉；验证与同步证据新增
  `delegated_publisher_ids`，委派变更受根签名、有效期和 sequence/digest 约束。
- Windows、WSL、Linux、macOS、Termux 一键安装现在可在同一命令中原子传入多个
  本地 control publisher：Windows 使用 `-ControlTrustedKey id=key`，POSIX/Termux
  重复使用 `--control-trusted-key id=key`。旧单 key 参数保持兼容；冲突归属在安装前
  拒绝，第一个 key 固定为根页面 `root_key_id`，其余 key 只能签被点名的嵌套来源；
  Deployment Receipt 新增完整 `control.key_ids`。
- 嵌套 remote-control `pages`/`kv` 来源现在可使用 `{url,key_id}` 精确固定社区
  发布者。key 必须预先存在于本机 `trusted_keys`，子页必须由该 key 签名；即使另一个
  发布者同样受信也不能冒充。`control-verify`、同步结果和私有状态新增
  `source_publishers` 归属记录，旧字符串 URL 格式保持兼容。
- 新增跨平台两阶段 Deployment Continuity Gate v1。`continuity-prepare` 在重启前
  固定健康 supervisor 实例、启动会话、Node ID 与 identity/TLS 哈希；
  `continuity-verify` 在重启后要求新实例、新同步、身份材料不变，并可用
  `--require-boot-change` 强制验证真实 OS/WSL/Android 启动会话变化。成功 challenge
  只能消费一次，完整 challenge/receipt 均为本机私有证据。
- TLS 完整性检查现在同时验证证书公钥与 `tls-key.pem` 私钥匹配，避免只检查证书
  Common Name 却接受不配对密钥。
- 新增持久 `Supervisor Health v1`：supervisor 原子记录心跳、首次/最近控制页同步、
  子进程 PID、退化错误和连续失败次数；`anet supervisor-status` 只有在心跳新鲜且
  supervisor/`anet serve` 两个进程都存活时返回成功。所有 one-click 安装器现在等待
  这份证据并把它嵌入 Deployment Receipt，避免仅凭原生服务管理器的 `Running`
  状态误报安装完成。
- Windows、WSL、Linux、macOS 和 Termux 持久 one-click 安装器现在统一输出
  Deployment Receipt v1：版本化 JSON 接口分别报告 runtime、独立节点、已验证
  控制页、原生 supervisor 状态、autostart 与 preflight。Agent 必须校验必需字段，
  且完整收据因包含 Node ID、路径、地址和控制 URL 而只能作为本地私有部署证据。
- 新增 `scripts/bootstrap_posix.py` checkout-free bootstrap。WSL、Linux、macOS 和
  Termux 可通过一条 `curl | python3` 命令下载临时平台入口，复用现有重复检测、
  控制页校验和原生服务注册流程；已有 checkout 时仍可直接运行平台脚本。
- 部署预检现在同时检查显式 `--node-home`/`-NodeHome` 和 `ANET_HOME` 指向的
  已有持久节点目录，避免自定义节点 home 绕过 Anet 重复安装检测；runtime-only
  安装仍不读取持久节点标记。
- checkout-free bootstrap 不再使用未验证控制页的 `repo_url`/`repo_ref` 选择并执行
  辅助脚本；POSIX 默认从官方 Anet `main` 获取，Windows 默认使用官方 helper
  repository/`-GitHubBranch`，fork 入口必须由操作者显式指定。
- Core CI 现在也会在任意分支 push 时运行，优化分支不再依赖手动 workflow
  dispatch 才能获得跨平台测试和打包结果。
- GitHub Actions 的 Python runner 已升级到 `actions/setup-python` v7，消除
  Node 20 runtime 弃用警告并保持 workflow action 使用固定 SHA。
- 修正 Windows、POSIX 初始安装器和运行中 remote-control 对顶层
  `default_config` 的 Windows/WSL 端口校验，并让 Windows PowerShell overlay 的空
  JSON 对象保持公共嵌套配置；错误的平台结构现在会在安装早期失败。
- 一键部署入口现在与远程 supervisor 对 `config`/`software` 平台 overlay 使用相同的递归合并和
  `default_config` 语义；首次安装不会因嵌套字段而与后续控制页同步产生不同结果。WebDAV
  carrier 的探测回归测试区分唯一 mailbox 路径与安全的传输重试，避免在全量 CI 负载下误报。
- 远程控制页新增可选的本地 Ed25519 发布者固定：配置 `trusted_keys` 后，根页和
  嵌套 `pages`/`kv` 页必须签名并通过有效期校验；签名序列号不能复用到不同内容。
  Windows、WSL、Linux、macOS 和 Termux 一键部署入口均可写入公钥策略，并新增
  `scripts/sign_control_page.py` 供离线发布者生成页面；旧的无签名页面仍保留为
  明确的兼容 bootstrap 模式。
- 新增只读 `anet control-verify`。所有跨平台 one-click 安装器在注册持久服务前调用它，
  验证完整控制页、嵌套来源、Peer Card 和 Windows/WSL 端口策略，同时不提前写入
  `remote-control-state.json`，避免跳过首次软件更新。
- 签名控制页现在要求 wheel 首次安装和后续更新提供有效的 64 位十六进制
  `software.sha256`；签名 `repo_url` 仍可作为明确的源码安装路径，无签名兼容模式保持旧行为。
- Termux 一键部署现在与其它平台一致执行 wheel hash 门禁；所有平台都拒绝用命令行
  hash 覆盖控制页声明，并修正空 `wheel_url` 误判为 wheel 更新的情况。
- Mutual Actor-to-Actor relationship claims now have a signed participant
  withdrawal path. A withdrawal deactivates only the portable claim and records
  a content-free local activity fact; it never automatically changes a local
  Subject hypothesis, circle, contextual trust, PeerBook trust, capability or
  authorization. This makes `family` / `mutual-guardian` claims reversible
  without treating social distance as an authority grant.
- 新增受众绑定的 `social.relationship.disclosure` 加密对象、
  `relation-disclose` / `relation-disclosure-list` CLI，以及默认关闭的
  `anet_relation_disclose` / `anet_relation_disclosures` MCP 工具。披露只接受
  关系活动的无正文字段，重复绑定 Packet 发送者和唯一接收者；可信接收结果进入
  独立观察账本，绝不折叠进接收方自己的 Actor、Subject、圈层、上下文信任、
  PeerBook trust 或 authorization。
- 关系书升级为 v7；圈层与上下文信任事件开始保存当时的最小结构化值，
  旧 v1–v6 关系书继续加载且不臆造历史。新增 observer-bound `rac_` 游标和
  `relation-activity` 增量/长轮询读取，将 Actor 事实、Subject 推测、互动、
  关系估计与建议决定投影为同一条无正文活动流。流按持久化追加顺序而非时间戳
  排序，证据引用与决定理由只输出摘要，权限影响恒为零。
- 新增默认关闭的 `anet_relation_activity` MCP 工具供长期 Agent 高频读取；
  只有显式设置 `ANET_MCP_ALLOW_RELATION_ACTIVITY=1` 才能访问本机私有关系流。
  `/social` 导入 v7 模型后可回放真实活动，示例中的采纳/拒绝也会即时加入时间线。
- 关系书升级为 v6，新增观察者本地、不可变的建议采纳/拒绝历史。
  `relation-decide` 只接受当前证据仍可复现的建议 ID；采纳时关系变更、决定记录
  与事件一次原子保存，拒绝时不改关系。证据变化会使旧建议失效，任何决定均不
  改变 PeerBook trust、Subject 归并、capability 或 authorization。
- `/social` Demo 新增建议采纳/拒绝交互、决定审计记录和 v6 本地模型导入，
  用可见流程区分“互动证据、关系建议、显式决定、关系变化”。
- 网站新增 `/amesh` 产品层页面：Amesh 作为 Anet 之上的只读 Agent 社交层视图
  （ANET CORE → ANET SOCIAL → AGAME OVERLAY → HUMAN LENS），并引入
  `bilingual` 双语 `T` 组件；agent-social 与 blog 页面双语化，站点导航将
  Amesh 置于 Updates 之下。`/amesh` 只作产品视图，不授予权限、不建立信任。
- 真实 Discord 验收证据：独立 WSL 测试节点经 `discord-social-config` 绑定
  Guild/channel，bot token 仅从 `ANET_DISCORD_BOT_TOKEN` 环境变量读取；REST
  v10 polling 对新消息递增 `ingested`，非 mention 消息按 metadata 脱敏保存，
  显式 bot 用户 mention 保存 ≤2000 字符正文并标记 `interaction:mention`，
  证据加权出现 `bounded mentions +2`；未达 reply 门限时 `discord-social-reply`
  fail-closed 拒绝。角色 mention（`<@&...>`）不进入 Discord `mentions[]`
  数组，v1 按 metadata 处理，属已知语义边界。
- 真实公网 Ahub 锚点验证：`ahub-serve` 经 Cloudflare Tunnel 命名隧道暴露为
  `https://ahub.905527.xyz`，两个 disposable WSL 节点经公网完成 Rendezvous、
  Live Relay 与密文跨公网交付（inbox 收到测试正文），Ahub 日志仅含聚合
  route 类、无 Node ID 或正文。

## 0.12.1 — 2026-07-25

- 新增 Windows、WSL 与 macOS 纯平台安装器。默认安装只建立版本化 Anet runtime，
  不创建节点、不读取 Hermes、不假设 profile、不注册服务；现有 node home 与
  supervisor 的升级另由显式发布门禁处理。Windows 停止态节点门禁与 WSL
  多节点原子门禁均属于可选部署层，不定义产品默认环境。
- WSL 默认安装根从当前部署使用的 XDG 数据路径中解耦为 `~/.local/anet`；
  Windows 默认使用 `%LOCALAPPDATA%\Anet`，macOS 默认使用
  `~/Library/Application Support/Anet`。三个入口均校验固定 wheel 哈希并安装到
  版本化 runtime，不隐式创建 `ANET_HOME`。
- 将可选的 `wsl_shared_release_gate.py` 更名为
  `wsl_multi_node_release_gate.py`，删除 shared/profile/Hermes 默认语义；所有
  node home 和 supervisor service 必须由部署方显式传入。
- 三平台安装器新增隔离的 `core/mcp/full` feature，Agent 可直接安装带 stdio
  MCP 依赖的版本化 runtime，不再对已安装环境追加临时 pip 变更。新增 CLI/MCP
  Agent 自助指南、fail-closed 通用配置模板和“所有注册工具必须进入文档”的测试。
- 新增自包含 Hermes `install-anet` Skill：内置固定 0.12.1 wheel 与 SHA-256，
  在全新 Linux 用户 HOME 下安装独立 MCP runtime，验证 CLI/MCP 且默认不创建
  身份。另提供可直接发送给新 Hermes Agent 的单提示词模板；跨电脑使用仍要求
  Skill 所在仓库先发布到目标机可访问的 GitHub/HTTPS 地址。
- GitHub 首发准备移除公开树中的本机路径、完整 Node ID、profile/角色名和原始
  部署证据，并将 wake bridge 源码改为 runtime-neutral。新增 Apache-2.0 根
  许可证、贡献/安全/发布指南、三平台核心 CI、Dependabot、固定 SHA 的 Actions
  与自动脱敏/Agent-neutral 仓库边界测试。

## 0.12.0 — 2026-07-25

- Ahub Relay 在服务重启后遇到“预约已可发现、owner listener 尚未重新挂接”的
  短暂 transport 错误时，会在 carrier timeout 边界内按 listener retry interval
  重试；认证、配置等不可恢复错误仍立即失败。
- 将中立的 Rendezvous/Mailbox/Relay 基础设施统一命名为 Ahub：模块、类型、
  CLI、Carrier 配置、协议字段、测试和运维文档一次性迁移为 `ahub`。Ahub
  服务端不再由 `import anet` 或 `anet.carriers` 隐式加载。
- 清除源码和部署资产中的特定 Agent/旧组织角色：Windows 脚本强制显式传入
  `NodeHome`，macOS bootstrap 强制传入 label，systemd 改为
  `anet-node@.service` 参数化实例。新增仓库边界测试阻止角色名重新进入。
- 根据当前部署现实把主线从 Android Companion 转到 WSL Discord Social：
  两台手机暂由 Android Remote Control MCP 1.9 承担交互，已有 Companion 协议
  与实验 App 保留但暂停继续开发。
- 新增 exact-field `social.discord.signal`、证据加权信誉分、独立置信度、
  `observe/surface/reply/amplify/connect_candidate` 单调门限及 operator/adapter
  标签 namespace；`connect_candidate` 明确不能创建 Peer trust 或 capability。
- 新增 WSL 内置 Discord REST v10 bridge：只轮询显式 Guild/Channel allowlist，
  启动时复核 Channel 属于目标 Guild；默认只保留明确提及 Bot 的正文，其余事件
  metadata-only，Discord actor/Guild/Channel 在 Anet signal 中均为 HMAC 假名。
- 新增私有 SQLite social ledger，持久保存不可变事件、actor 有界证据、人工标签、
  cursor、Anet route 和 Discord reply 状态；Anet queue 失败时不推进 cursor，
  重试不重复累计 actor 证据。Bridge 嵌入已有 `anet serve`，不启动第二个借用
  同一 node home 的 runtime。
- Discord 回复要求当前 actor 与原事件通过 reply 门限，禁用全部 mention 解析、
  绑定原消息、启用 `enforce_nonce` 并拒绝同一事件的不同第二回复；HTTP 429
  遵守 `Retry-After`，bot token 只来自环境变量。
- 根据主力手机“Humon 主要感知节点 + Node B 主要作用界面”的更新定位重排路线：
  P0 Companion 从通知/授权单向界面改为感知与干预双向最小闭环，并把低风险
  Observation、Self Report、端侧隐私过滤和 UserResponse 从 P2 前移到 P0.3；
  Body/Environment/Social Sense 的完整权限扩展仍留在 P2。
- 新增严格 `anet.companion v1`：定义 ObservationBatch、Episode、Intervention、
  UserResponse、ApprovalRequest 和 ApprovalDecision 六类对象。P0 只接受电量、
  粗粒度网络、低频 presence、用户主动 Self Report 和逐项授权应用类别时间窗。
- Companion validator 拒绝未知字段、未定义 kind、二进制传感器内容、原始事件、
  精确位置、通信正文、健康明细和 HumanState/诊断/情绪标签；AnetNode 在发送前
  和解密落盘前执行门禁，不能靠绕过高层构造器把禁用字段放进可信 Inbox。
- Approval request/decision 绑定 Human ID、明确 Device Node ID、capability、
  resource、参数摘要、once/bounded scope、短 TTL 和 nonce；Packet sender 与
  对象 source/target device 在发送和可信 Inbox 前再次绑定，不能把合法对象移接
  到错误设备。
- 新增 Node B 侧高风险审批执行闸门：本地请求/nonce 登记、可信 Decision claim
  激活、当前 `HumanDeviceGrant(approval.sign)` 与终局撤销复核、次数限制、稳定
  effect idempotency key、worker lease takeover、旋转 execution token 和旧
  token fencing 均持久化并可跨重启恢复。任意外部 executor 仍必须使用稳定幂等键，
  不能把本地 SQLite 账本误称为跨系统 exactly-once。
- 新增 `control-import`/`control-device`，只验签导入和查询 node-owned 控制库的
  公开 Descriptor/Grant/Revocation，不复制任何身份私钥；三个 approval MCP 工具
  仅在 `ANET_MCP_ALLOW_APPROVAL_EXECUTION=1` 时开放。
- 新增六份语言中立 JSON fixture，以及协议、节点、审批崩溃恢复、篡改、nonce
  重用、错误能力、撤销、拒绝、并发 fencing 和 CLI/MCP 默认关闭测试，供
  Android/Kotlin 实现做互操作门禁。
- 新增 Android-independent Kotlin `mobile/companion-core`：六类对象执行与
  Python 相同方向的 exact-field、类型、TTL、隐私禁止字段、consent、approval
  binding 和 Packet endpoint 校验；七项 Kotlin 测试使用仓库同一组 fixture。
- 新增跨语言 `canonical-sha256.json`，Python 与 Kotlin 分别对规范化、排序、紧凑
  UTF-8 JSON 计算相同 SHA-256，避免两个实现各自通过但归一输出漂移；新增按路径
  触发的 Companion Conformance CI。
- 新增最小 Android `mobile/companion-app`（AGP 9.2.1/API 37.0/Room 2.8.4）：
  嵌入 `companion-core`，用 Room 持久化 consent、加密 Outbox 和 Intervention/
  Response 状态；Android Keystore AES-256-GCM 只保存不可导出密钥，AAD 绑定记录
  类型、语义 ID、消息类型与时效。consent 撤回删除未发送密文，Outbox 使用旋转
  claim token/lease fencing，Response 在同一事务中校验干预动作并入队。
- 新增 4 项 Android 主机测试，覆盖密文落盘与 AAD 篡改拒绝、幂等与过期 lease
  接管/旧 token fencing、授权撤回清理和复用拒绝、干预去重及响应原子入队；
  Companion CI 同时构建 Debug APK。系统通知、前台服务、网络/身份和真机
  Keystore 行为仍是明确未完成项。
- 新增 `docs/EIGENFLUX_MATRIX_REFERENCE.md`：EigenFlux 只启发 P2 可选的
  public-safe Signal Discovery Plane，匹配不授予 capability；Matrix 只启发
  event/state 分离、设备撤销、严格 schema、同步 cursor/gap/backfill 和
  Companion 分层，不在 P0/P1 引入聊天 room、homeserver 身份或完整 DAG 联邦。
- 新增无私钥公网锚点核心：本地完整 Node ID allowlist、签名请求认证、持久 nonce
  防重放、Rendezvous 和只保存现有 `SealedPacket` 原始密文的 Mailbox。
- Mailbox 使用有界配额、TTL、精确 Packet ID 冲突检测和领取租约；只有目标节点
  在本地持久化后用 claim token settle 才删除。上传响应明确标记为
  `ahub_custody_only`，不冒充目标送达或端到端完成。
- 新增框架无关 ASGI API 与默认强制 HTTPS 的标准库客户端；覆盖离线领取、锚点
  重启、nonce 重放、租约恢复、密文不变、越权、配额和并发唯一赢家。真实手机
  链路仍未实现。
- Ahub 数据库升级至 v6，新增持久、peer-scoped Relay reservation；同一
  owner/peer 刷新保持 ID，TTL、每 owner 数量、session duration 和双向 byte
  allowance 全部受服务端上限约束。
- 新增双方 `AhubRequest` 签名认证的 WebSocket live byte Relay；owner/peer
  都只主动出站，reservation ID 不能替代 Node ID 授权。转发只接受 binary frame，
  每次 ASGI send 提供 backpressure，并按 frame、方向字节、时长、expiry、
  reservation 和节点并发关闭双方。
- 新增异步 `AhubHTTPClient.open_relay` 与协商后 binary connection；禁用系统
  HTTP proxy、User-Agent 和 WebSocket compression。Uvicorn 同步限制 frame 且
  继续关闭 access log、proxy headers 和 server banner。
- 真实独立进程测试覆盖 reservation 跨 Ahub 重启、双方双向字节、第三节点越权、
  nonce 重放、字节/时长上限和日志脱敏。
- 新增 Relay↔socket 背压桥与 `RelayTLSWriter` 生命周期；显式节点 API 在临时、
  未广告的 loopback listener 内复用现有 TLS 1.3、证书 channel binding、签名身份
  握手、sync 和 receipt，不让 Ahub 终止 TLS。
- 两个关闭常驻 listener/direct 的 disposable AnetNode 经真实 Ahub 进程完成
  Packet、目的 ACK、业务 receipt 和双方 pending 收敛；路径准确记录为
  `ahub-relay:*`，重复 session 保持单 Inbox，关闭后无命名 Relay task 泄漏。
- 新增 `--live-relay` 配置与边界参数；运行时按明确 peer 维护持久 owner
  reservation/listener，采用抖动退避恢复。签名 discovery 只返回 caller 被授权
  加入的 owner reservation，不提供枚举。
- AdaptiveRouter 的 Ahub 复合路径先尝试现有 TLS/sync Relay，并独立记录
  `ahub-relay:*` 指标；随后保留 StoreCarrier 处理离线 Mailbox。真实进程测试
  证明 Ahub 终止/重启后 listener 自动重建、第二条业务 Packet 再次经 Relay
  收敛，且业务 Packet 未被 custody 状态误判。
- 完整回归为 228 项非 WebDAV + 4 项 WebDAV；Ruff、compileall 和 diff-check
  通过，diff-check 仅保留既有 `.gitignore`/`pyproject.toml` LF→CRLF 警告。
- 新增 Ahub operator CLI：独立 root 的 allow/disallow/list/status/purge/
  checkpoint，以及 loopback-safe 单 worker `ahub-serve`。节点 home marker
  会 fail closed，非 loopback 绑定要求显式 opt-in。
- Ahub allowlist 支持持久 disable/re-enable，停用不删除待过期密文；数据库
  v1→v2 原地迁移保留既有节点。健康端点检查两库，聚合状态不列 Node ID，HTTP
  日志仅记录稳定路由类别和有界数值。
- 新增真实 HTTP 子进程回归：上传密文后终止并重启 Ahub，目标节点仍能领取、
  解密和 settle，进程日志不含 Node ID 或正文。
- 新增自适应 `AhubCarrierConfig`/CLI/路由接入：节点自动发布持久公开 descriptor
  revision，按 pinned peer 领取，depth-zero Packet 直达最终目标；不支持的多跳
  Packet 不会被错误降级。
- PacketStore 新增 path-only `custodied` 状态。Ahub 上传成功不完成全局
  delivery，也不阻止其他直连/Carrier；只抑制同一 Ahub 路径重复上传。
- 新增目标签名 `DestinationSettlement v1`：绑定 Packet ID、raw SHA-256、
  uploader、destination 和 expiry。Ahub 只能保存/返回证明，发送端用 pinned
  key 与本地 raw 验证后才标记目的 ACK；业务 receipt/consumer ACK 保持独立。
- settlement proof 采用本地落盘后显式 ACK；已 ACK 证明退出有界轮询队列但去重
  tombstone 保留至 Packet expiry，避免超过 batch 后新证明饥饿或旧密文重入。
- 新增真实双节点 Ahub StoreCarrier 回归：禁用直连，经独立 HTTP 进程上传，
  强制重启 Ahub，随后目的 Inbox、签名 settlement、业务 receipt 和双方 pending
  全部收敛，重复轮询不产生第二条 Inbox。
- 新增 `docs/AHUB_OPERATIONS.md`：TLS 反代、独立状态根、服务加固、指标、清理、
  停机 checkpoint 备份和恢复边界。
- 新增 `docs/AHUB_V1.md`，记录 Ahub 可见元数据、认证域、配额、custody/receipt
  边界，以及从 EigenFlux 与 libp2p 只吸收网关验证和有界中继资源管理的原则。
- 新增无网络副作用的控制平面模型：地址无关 `NodeDescriptor v2`、15 分钟内
  短期 `ReachabilityRecord v1`、独立人类主体 `HumanDeviceGrant v1` 和终局
  `HumanDeviceRevocation v1`；现有 PeerCard v1、Node ID 与 trust pin 不变。
- 控制平面对象使用独立签名域、严格字段集、有界有效期及
  `sequence + previous_digest` 修订链；新增 tracker 覆盖幂等重试、回滚、跳号、
  同序分叉、旧 session 重放、陈旧 descriptor、错误签名和撤销后再授权拒绝。
- 新增 SQLite `ControlPlaneStore`：使用 `BEGIN IMMEDIATE` 原子提交公开对象与
  checkpoint，跨重启保持旧 session/回滚拒绝和终局设备撤销；并发同序分叉只有
  一个持久赢家。数据库不含节点或人类主体私钥。
- 新增 `docs/CONTROL_PLANE_V1.md`，明确动态地址、人类身份、设备身份和外部聊天
  账号的边界；该切片尚未接入 ahub API、PeerBook/CLI 或任何部署节点。
- 新增 `anet wake-bridge`：以 loopback token 认证的无正文 edge hint 唤醒 Hermes，不暴露消息内容。
- 新增 Hermes Anet platform：使用独立版本化 session，直接领取 durable consumer batch，正常 turn 后 ACK、异常时 NACK，入站处理不依赖 MCP。
- FastMCP 启动时禁止隐式读取 profile `.env`，避免无关或非 UTF-8 dotenv 破坏 Anet MCP 生命周期。
- 增加 wake、MCP 启动和 Hermes platform 回归测试，以及 `docs/HERMES_WAKE.md` 运维与缓存说明。
- 新增版本化 `agent.task.request/status/result/cancel` 信封与单一发送工具 `anet_task`，为 A2A、Hermes、Codex 和后续发现/匹配网关提供确定性任务边界。
- 新增持久 typed task 执行账本：以 consumer group、认证发送者和 task ID 组成幂等键，并绑定规范请求摘要；相同重试返回既有状态，不同正文复用 task ID 时 fail closed。
- 新增 `anet_task_begin / anet_task_settle`：执行 token 随 claim 租约接管而轮换，结果持久化与 consumer ACK 同事务；进入 task ledger 后不能用通用 settle 绕过该原子边界。
- 新增 fail-closed 入站任务策略：`ANET_MCP_TASK_ALLOWED_SENDERS` 与 `ANET_MCP_TASK_CAPABILITIES` 分别约束认证发送者和执行能力；精确项、显式 `namespace.*` 与全局 `*` 语义分离，模型参数不能扩权。
- task policy 在幂等账本写入前执行；拒绝不会创建执行记录。MCP status 只显示 sender 数量和 capability pattern，不例行输出授权 Node ID。
- 新增纯函数式 A2A 1.0 边界层：生成 `supportedInterfaces` Agent Card，把认证 sender × `messageId` 确定性映射为 Anet task ID，并转换标准 Part、TaskState、status/artifact stream value。
- A2A skill 的公开描述与本地 `required_capabilities` 显式分离；请求 metadata 不能扩权，远端明文 endpoint、隐式匿名发布、tenant 错配、旧/歧义 Part 和缺少持久映射的 follow-up task 默认拒绝。
- 新增 SQLite A2A gateway 映射：principal 经显式 sender allowlist 固定到单一 Node ID；task 固定 context、tenant、目标 peer、skill 与协议版本；每个 message 绑定独立内部 Anet task 和规范请求摘要。
- A2A 重试在 `BEGIN IMMEDIATE` 事务中去重；相同 `messageId` 改正文、跨 task 复用内部 task ID，以及 follow-up 改 sender/context/tenant/peer/skill/version 均 fail closed；映射跨进程重启保留。
- 新增 A2A task 聚合状态与 append-only 事件日志：status/artifact 经结构验证后分配单调 sequence，重复事件幂等，旧 follow-up 执行结果被 fencing，认证 principal 可用 cursor 跨重启续读。
- 新增 A2A dispatch outbox：message 映射与规范 `agent.task.request` intent 在同一事务落盘；本地 worker 使用有界租约和 fencing token，Packet 插入、peer-scoped OPK 使用与 dispatched 状态同事务提交。
- outbox 的稳定 encryption reservation ID 可在进程崩溃后找回同一预密钥；失败重试会原子烧毁旧预留并轮换 ID。接收端继续以认证 sender × task ID 去重，因此 transport Packet 重试不会重复业务副作用。
- 新增 A2A CancelTask 映射：认证 principal、tenant 与外部 task 必须匹配；一次取消为该 task 下每个内部 Anet task 创建持久 `agent.task.cancel` dispatch，并禁止后续 message 扩展。
- 新增 typed task cancellation ledger：取消先到时保存 tombstone；运行中进入 `canceling`，仅允许 canceled settlement；worker 可查询持久取消原因，失联后由 request lease takeover 清除旧 execution token。
- 完成与取消事务竞态只有一个持久赢家：完成先提交则取消记为 `too_late`，取消先提交则后续完成失败；A2A 仅在收到 canceled 结果后把 `cancel_state` 从 dispatched 变为 confirmed。
- 新增 `docs/SIGNAL_REFERENCE.md`：把 Signal 的异步 bootstrap、ratchet、Sesame 多设备、Sealed Sender 与 key transparency 映射到 Anet，并明确独立 Node home、Agent capability 和 AGPL 代码许可边界。

## 0.11.0 — 2026-07-18

- 新增本地 `stdio` 直连拨号器：外部进程只提供可靠双向字节流，Anet 仍在其内部终止 TLS 1.3 并执行证书通道绑定、双向签名身份握手、Packet 同步和 ACK。
- 适配器通过 `create_subprocess_exec(argv)` 启动，不经过 shell；可执行文件必须是绝对路径，参数拒绝 NUL/换行，数量与长度有界。
- 子进程只继承平台运行必需的最小环境和管理员显式列出的变量名；目标 host/port 由保留环境变量注入，配置、列表和日志不打印变量值。
- 增加 `adapter_config / adapter_spawn / adapter_exit / adapter_timeout / adapter_protocol` 失败类别；hedged dialer 竞速取消会关闭 TLS、管道并精确终止落后 adapter，不把本地取消记为网络失败。
- 新增真实双节点、双向消息/ACK、环境隔离、早退、缺失可执行文件、竞争取消及孤儿进程检查；全量回归为 125 项通过。

## 0.10.0 — 2026-07-18

- 新增 `carrier_replica_count`（1–4）：选中异步 fallback 后，把同一不可变 Packet 有界复制到评分最高的多个健康 Carrier；默认 1 保持旧流量行为。
- 直连第一次失败但尚未达到完整 failover 阈值时，`control`/`interactive` 也可按相同副本预算抢跑到多个 Carrier。
- 显式 `mode=always` 继续优先于副本预算；Packet ID、Inbox 唯一约束和跨 Carrier ACK 保持 at-least-once 下的业务幂等。
- 新增配置上下界、健康 Carrier 选择、紧急 QoS 复制和两个独立 Directory Carrier 同密文投递/单 Inbox/最终 Pending 收敛回归；完整测试增至 119 项。

## 0.9.0 — 2026-07-18

- 新增 capability-gated `link-health-v1`：完成 TLS 与签名身份握手后仅交换 health frame，不传业务 Packet、receipt 或 Inbox。
- 新增 `dialer-probe PEER [--dialer NAME] [--require-all]`，逐个验证有效 dialer/locator，并把结果写入独立 `health:*` 指标。
- 新增代理配置、认证、不可达、超时、目标拒绝、DNS、TCP、TLS、身份握手、旧节点不支持和 health 协议故障分类；普通同步错误也带阶段前缀。
- 新增真实认证探针零业务状态、关闭代理分类、身份拒绝和旧 capability 安全降级回归；完整测试增至 115 项。

## 0.8.0 — 2026-07-18

- 新增 `direct_race_width`（1–4）和 `direct_race_delay`（0–5 秒），支持有界、延迟启动的候选路径预热；默认宽度 1 保持旧连接行为。
- 同批候选按既有 path score 启动，首个完成完整认证同步的路径获胜，落后任务被取消并关闭；同批全失败后才进入下一批。
- 取消不计为链路失败，避免把本地 hedging 调度污染为远端故障；真实失败和成功仍分别写入细粒度 dialer/locator 指标。
- 新增快速备用获胜并取消慢路径、真实双连接竞速不重复 Inbox，以及 routing CLI 持久化回归；完整测试增至 111 项。

## 0.7.0 — 2026-07-18

- 新增显式 `direct_dialers`：同一 peer/locator 可同时配置 raw、SOCKS5 与 SOCKS5H，并保持旧无代理和旧 `direct_proxy` 配置语义不变。
- 每个 `dialer × locator` 使用独立路径 ID、EWMA 延迟、成功/失败和连续失败指标；达到阈值后短暂冷却并切换其他拨号器，冷却到期自动恢复探测高优先级路径。
- 新增 `dialer-add / dialer-list / dialer-set / dialer-remove`，首次显式配置会物化旧有效拨号器，代理凭据继续只保存环境变量名。
- 状态与 doctor 显示有效拨号器；direct 已启用但全部显式拨号器禁用时给出警告。
- 新增旧配置迁移、显式配置 round-trip、raw 阻断→代理切换→raw 恢复和 CLI 不泄密回归；完整测试增至 109 项。

## 0.6.0 — 2026-07-18

- 新增签名作用域定位器：`host`、`lan`、`wan` 与优先级；只有本地持有匹配不透明 context 才尝试 host/LAN 地址，旧无参数地址继续兼容。
- 新增 `locator-config`，原子更新 context/广告地址并重签 `card.json`；`doctor` 报告 legacy loopback 和 context 不匹配警告。
- SOCKS5 本地 DNS 现在遍历去重后的 IPv4/IPv6 候选，每个失败候选使用新代理连接重试，修复 Windows `localhost` 首选 `::1` 而目标仅监听 IPv4 时的错误失败。
- 更新 Mac bootstrap 与 Node C handoff：发布 SHA-256 必须逐次显式核对，Mac 只共享 LAN zone，不能共享 Windows/WSL host zone。
- 新增 `scripts/wsl_release_gate.py`，把 WSL artifact 哈希、隔离测试、Ruff、固定 wheel 安装、systemd 重启、身份/PeerBook/撤销/预密钥代际/权限对比和失败回滚收敛为单一确定性门禁。
- 门禁始终写入 `0600` 完整 JSON 证据，但 stdout 只返回版本、三个健康状态和回滚结果，避免把 Peer Card 与大段状态送入 Agent 上下文。
- 新增 sdist 路径穿越/链接拒绝、哈希、pytest 计数、私密原子报告和最小公开摘要测试；Windows 完整回归增至 76 项，Ruff 覆盖门禁脚本。

## 0.5.2 — 2026-07-17

- TLS 直连可选经严格 SOCKS5/SOCKS5H CONNECT 建链；隧道内继续使用原 TLS 1.3、证书通道绑定与双向签名 peer 握手，不改变身份、信封、信任或撤销语义。
- 代理 URL 仅接受无凭据、无路径/查询/fragment 的 `socks5://` 或 `socks5h://host:port`；默认限回环地址，远端代理需持久化显式 opt-in。
- RFC1929 用户名/密码只通过成对环境变量名引用；配置、CLI 与状态不持久化或输出环境值。
- 新增 `direct-proxy` show/set/clear CLI 和非秘密状态元数据；修改标记需要重启。
- 新增本地真实异步 SOCKS5 relay 验证无认证、认证、DNS 模式、双节点消息/receipt 以及失败关闭和配置拒绝路径。

## 0.5.1 — 2026-07-17

- 新增本地 fail-closed 节点撤销清单；撤销记录先于正向信任删除落盘，进程崩溃后旧 Peer Card 也不能恢复信任。
- 新增 `peer-revoke` 的完整 Node ID 二次确认及 `peer-revocations`；普通 `peer-add`、配对完成和运行中握手均不能重新信任已撤销身份。
- 撤销会在单一 SQLite 事务中终止该 peer 的待发对象、把既有 Inbox 降为 untrusted、作废未完成 consumer claim、退休双方 peer-scoped 预密钥并清除路径状态。
- 撤销清单和 PeerBook 在 POSIX 上以 `0600` 保存；运行中的节点在出站、入站握手和消息验信边界即时重读撤销状态，无需重启。
- 验证覆盖崩溃窗口默认拒绝、撤销幂等、陈旧 claim 不可 ACK、预密钥不可再用、路由清理、错误确认拒绝，以及数据库清理中途失败的全事务回滚；Windows 完整回归 71 项通过。

## 0.5.0 — 2026-07-17

- 新增签名 `PairOffer`：包含随机 128-bit offer ID、本节点 Peer Card、创建/过期时间，默认一小时且最长七天。
- 新增签名 `PairResponse`：绑定原 offer ID 和完整 Offer 摘要，防止响应被移接到另一节点或另一轮配对。
- 新增 `pair-offer / pair-accept / pair-complete` CLI；只有明确执行 accept 才扩大接收端信任，完成端还必须持有自己签发的原 Offer。
- 配对对象与传输解耦，可通过 NATS、文件、二维码、局域网或人工携带交换；不新增中心账户、发现服务器或 MCP 自动授权入口。
- 验证覆盖签名篡改、过期、自配对、错 Offer 响应，以及两端 PeerBook 在完整 CLI 流程后的相互固定。

## 0.4.1 — 2026-07-17

- 首次直连失败且没有待发消息时，使用失败重试周期而不是空闲健康探测周期；对端稍晚启动时可快速恢复。
- POSIX 节点目录自动收紧为 `0700`，SQLite 数据库及 WAL/SHM 边车文件自动收紧为 `0600`。
- 真实后台双节点测试改用动态 localhost 端口和有界就绪等待，避免 Windows/WSL 并行运行时固定端口碰撞。
- WebDAV 故障转移回归改为有界状态驱动同步，验证实际投递、ACK 和主路径恢复，不再假设 HTTP worker 必须在固定四轮内完成。
- Windows 完整回归 64 项通过；WSL 路由测试连续 5 次通过且完整回归 64 项通过；Node A 重启后权限和服务健康已验证。

## 0.4.0 — 2026-07-17

- 项目品牌、Python 包、CLI、环境变量、MCP 工具、服务单元、运行目录和协议类型前缀由旧名统一改为 `Anet` / `anet` / `ANET`。
- 新离线包使用 `ANET-BUNDLE-V1`；导入端仍可读取旧 magic，保证已有人工携带介质不会失效。
- 节点首次加载时把旧 `ainet.sqlite3` 原子迁移为 `anet.sqlite3`，身份、Peer Card、预密钥库存和投递历史保持不变。
- v1 密码学域分隔标签保持不变；它们属于已部署 wire format，而不是公开品牌，避免改名导致节点 ID、旧密文和配对 Carrier 信道失效。
- 这是破坏性命名版本：旧 CLI、Python 导入、环境变量和 MCP 工具名不再作为正式入口。

## 0.3.8 — 2026-07-17

- 预密钥 bundle v2 将 Ed25519 签名绑定到唯一 `intended_peer_id`；本地私钥也记录获授权发送 peer，解密后再次核对签名身份。
- generation 从全局改为每个 peer 独立；Peer Card 声明 v2 后只选择 v2 库存，不会误用共享的 v1 密钥。
- 单-peer v0.3.7 数据库自动迁移并保留代际；多-peer 共享库存不做危险猜测，可用显式 `prekey-migrate --retire-shared` 擦除并为各 peer 建立安全 generation 基线。
- 新增运行时保留的 `network.prekey.request/bundle`，低于 waterline 自动请求、重发更新 bundle 或生成下一代；普通 CLI/MCP 不能伪造控制 kind。
- 控制对象优先使用 opk；双方 v2 库存均为零时，仅请求和签名公钥 bundle 可使用 v3 static 恢复，不改变普通消息的 `require` 策略。
- 新增 `prekey-config` 与 `prekey-replenish`；默认阈值 64、批量 256、请求间隔 900 秒、有效期 30 天。
- 拒绝请求方伪造超前 generation；30 秒内相同 request ID 不重复生成或排队响应，响应缓存有界。
- 新增 `packet_rejections`：AEAD、签名、格式和 peer 作用域等确定性失败停止 spool 重试；临时 I/O/SQLite 故障继续重试。
- 验证覆盖第三 peer 抢用、直连零库存、Directory-only 零库存、低水位二次补货、重复请求、v0.3.7 schema/Peer Card、单/多 peer 迁移和篡改终止；完整回归 61 项。

## 0.3.7 — 2026-07-17

- `SealedPacket` v3 新增受 AEAD/KDF 绑定的 `key_mode` 与 `prekey_id`；解析器继续读取 v1/v2。
- 新增由节点 Ed25519 身份签名的一次性 X25519 预密钥包，包含单调 generation、有效期、内容哈希和最多 1000 个公钥。
- 导入拒绝过期包、代际回滚、同代分叉和 ID 重用；SQLite `BEGIN IMMEDIATE` 保证多个并发发送者不会预留同一远端密钥。
- 接收端将 Inbox 写入、Packet 完成和预密钥私钥置空放在同一事务；提交前崩溃可恢复，提交后同包重放不依赖已擦除密钥，不同包重用相同密钥被拒绝。
- 新增 `prefer / require / disable` 策略以及 `prekey-generate / import / status / policy` CLI；发送 CLI/MCP 明确返回实际 packet version、key mode 和有限前向保密范围。
- 对方 Peer Card 未声明 `one-time-prekeys-v1` 时自动发送旧 v2 静态对象；`require` 模式拒绝能力不足或库存耗尽，避免静默降级。
- SQLite 启用 `secure_delete`，消费后请求 WAL truncate checkpoint；文档明确这不是对备份、快照、SSD、内存或已持久化明文 Inbox 的安全擦除保证。
- 新增签名/篡改、错身份、回滚/分叉、并发预留、一次性使用、重放、崩溃恢复、私钥逻辑擦除、过期清理、严格策略、CLI 与滚动升级测试；完整回归 51 项。

## 0.3.6 — 2026-07-17

- 新增 SQLite durable consumer group；`latest` 使用 Inbox rowid 边界，过滤条件创建后不可静默修改。
- 新增 `BEGIN IMMEDIATE` 原子 claim、随机 128-bit token、租约到期转交、续租、ACK、NACK 延迟重试和 delivery attempt。
- 同一 group 的并发 worker 竞争且不重复领取；不同 group 对同一消息独立 fan-out。
- claim 默认只接收可信、可见对象，并明确附带“来源认证不等于指令安全”的本地策略警告。
- 新增 consumer CLI：`consumer-open / claim / settle / renew / status`；人工 Inbox read 标志与 Agent 消费状态保持分离。
- MCP 新增五个 durable consumer 工具，总计 12 个 Anet 工具，并通过真实 stdio initialize/list-tools 握手。
- MCP 进程 capability 可固定 Agent owner、consumer group/kind 前缀、允许 peer，并可禁止 raw Inbox、untrusted 和 transient 输入；模型参数不能扩大范围。
- purge 清理孤立 consumer delivery；节点状态新增 group 和 active claim 计数。
- 新增 rowid 边界、过滤、并发 claim、租约恢复、陈旧 token、续租、NACK、fan-out、清理、CLI/MCP 与 capability 测试；完整回归 40 项。

## 0.3.5 — 2026-07-16

- 新增独立 `AdaptiveSchedule`：系统熵源随机 jitter、有界指数空闲退避、强制执行与新 Packet 即时唤醒。
- 空闲直连不再每个 2 秒本地 tick 建立连接；默认 60 秒健康探测、±35% 抖动、最多 4 倍退避。
- 待发 Packet ID 增量会立即唤醒路径；同一未确认 Packet 不会在每个 tick 重复绕过退避，失败重试默认从 5 秒开始。
- Directory/WebDAV 后台轮询默认 ±25% jitter、最多 4 倍退避；实际收发活动会重置退避。
- fallback 直连恢复探测加入独立 jitter 和失败退避，继续保持非阻塞 Carrier 数据面。
- 全局本地 sync tick 加入 ±20% jitter，避免远端请求仍被固定时间栅格量化；显式 sync/probe/benchmark 保持即时执行。
- `carrier-add`、`carrier-serve` 与 `routing-config` 增加调度参数，节点状态展示生效的 interval/jitter/backoff。
- 新增调度数学、即时唤醒、Carrier backoff 和真实双节点空闲直连测试；完整回归 34 项。

## 0.3.4 — 2026-07-16

- Directory/WebDAV Carrier v2 按 7 天 epoch 分别轮换每一对节点、每个方向的邮箱 token、帧密钥和命名密钥。
- 当前、下一时钟偏差窗口和五个历史 epoch 覆盖 31 天离线帧保留期；接收端继续读取 v1 静态邮箱。
- 发送方依据对方签名 Peer Card 的 `directory-carrier-v2` / `webdav-carrier-v2` 能力协商格式，支持不中断滚动升级。
- v2 去掉 `ch-` 和 `.drop` 特征，使用不透明 collection 与 48 位 HMAC 对象名；隔离区也不再保留原邮箱 token。
- v2 Carrier 明文在二次加密前填充到至少 4 KiB 及后续 2 的幂次桶，使 ACK 与常见小消息具有相同线上对象大小。
- WebDAV 改为一次枚举基础 collection，只读取已存在且可验证的邮箱，不会通过逐 token 探测向服务端暴露整组轮换值。
- 增加跨 epoch、防跨 epoch 重放、长度桶、v1 降级和 WebDAV 请求面测试；完整回归 29 项。

## 0.3.3 — 2026-07-16

- 新增带随机调度抖动的长期 `anet monitor`，每条观测立即 fsync 到本地 JSONL。
- 新增 Windows monitor 启动、状态和安全停止脚本，PID 指向真实解释器进程。
- 默认低频探针间隔 60 秒、jitter 25%；部署试运行使用 35%。
- transient Inbox 默认保留 7 天，并清理孤立 delivery/receipt 记录，限制持续观测造成的数据库增长。
- 明确监测流量和路径日志都是可关联的敏感元数据，不作为隐蔽流量功能。
- 完整回归 26 项通过。

## 0.3.2 — 2026-07-16

- 新增多备用 Carrier 失效转移：当前 fallback 达到失败阈值后选择下一条健康路径，首选路径恢复后经连续成功与冷却切回。
- fallback 状态下的直连恢复探测改为后台低频任务，不再阻塞 Carrier 数据面。
- probe 发现 Carrier 帧在途时，只轮询对应 Carrier 拉取 receipt，避免重复直连超时。
- 新增 `anet benchmark`，输出成功率、p50/p95、实际 ACK 路径、路由切换次数和逐探针 JSONL。
- probe/benchmark 支持最高 4 MiB 的机器随机载荷，用于带宽与大包路径实验。
- 新增可重复的 Linux network namespace/veth/netem 实验脚本。
- 完成 120±30 ms 延迟、50±10 ms+5% 丢包、1 Mbit/s 限速大包及 100% 单向黑洞实验。
- 完整回归 24 项通过。

## 0.3.1 — 2026-07-16

- 新增逐 Packet `delivery_paths`，区分 `direct`、`directory:<name>` 与 `webdav:<name>` 的尝试和 custody ACK。
- 新增 `anet probe` 与 MCP `anet_probe`，返回端到端耗时、实际确认路径及切换前后路由。
- probe 和 receipt 作为 transient 对象参与去重，但不再污染普通 Agent Inbox。
- 新增仅出站 WebDAV/HTTPS Carrier；凭据仅引用环境变量。
- 拒绝远端明文 WebDAV、URL 内嵌凭据和 HTTP 重定向。
- 本地 WebDAV 参考端完成 packet、ACK、receipt、receipt ACK 四轮测试。
- 完整回归 19 项通过。

## 0.3.0 — 2026-07-16

- `SealedPacket` v2 加入受认证的 `control / interactive / normal / bulk` QoS；解析器兼容 v1。
- 新增持久化路径指标、连续失败/恢复计数和成功 RTT EWMA。
- 新增 fast-fail/slow-recover 路由：高优先级消息先竞速，达到阈值后全量 failover，恢复需连续成功与冷却期。
- Windows/WSL 运行节点完成真实断开、自动切换、回执和恢复滞回验证。
- Windows 项目独立 `.venv`；WSL 使用持久 systemd user service。

## 0.2.0 — 2026-07-16

- 新增 Carrier 接口和无监听 Directory Carrier。
- 配对方向信道使用 X25519/HKDF、ChaCha20-Poly1305、Ed25519 与 HMAC 不透明文件名。
- 完成两个监听端口均关闭时的目录投递和回执验证。
- 修复目的节点 custody ACK 与中继 ACK 的完成语义。

## 0.1.0 — 2026-07-16

- 自持 Ed25519/X25519 身份与签名 Peer Card。
- 端到端 `SealedPacket`、TLS 绑定的双向挑战、SQLite WAL 存储转发、TTL、去重、有限跳数和 receipt。
- CLI、MCP、离线 Bundle、两节点直连和三节点离线中继。

## 版本声明

版本号表示代码和本地验证里程碑，不表示某版本已经“不可封锁”。具体抗封锁结论必须引用对应的 [VERIFICATION.md](VERIFICATION.md) 证据和测试环境。
