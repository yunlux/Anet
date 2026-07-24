# Anet 安全策略与边界

## 漏洞报告

发布到 GitHub 后，请使用仓库的 **Private vulnerability reporting** 提交安全
问题。不要在公开 Issue、Discussion、日志或聊天中粘贴密钥、完整 Node ID、
Peer Card、节点目录、地址、凭据或未脱敏证据。

当前维护线为 `0.12.x`。这是 Alpha 软件；修复时会优先保护协议兼容性和
fail-closed 边界，但不承诺固定响应时限。疑似凭据泄漏时应先在对应服务商处
撤销或轮换凭据，再处理代码与 Git 历史。

## Public Ahub 边界

P0.2 Ahub 使用与每个 `ANET_HOME` 分离的 deployment-owned root，不保存节点或
人类私钥。数据库仍向服务端 operator 暴露 allowlist、可达性、上传者/目标、
时间、大小和 claim 元数据；Mailbox 正文保持现有 `SealedPacket` 端到端密文。

Ahub HTTP 认证使用当前 descriptor key，并绑定 method、精确 path、body digest、
时间窗和持久单次 nonce。上传成功只表示 custody；只有目标节点能领取自己的
队列，并在本地持久化后用未过期 claim token 和签名
`DestinationSettlement v1` 删除。settlement 绑定 exact raw digest、上传者、
目标和有效期；发送端用 pinned destination key 验证，因此 Ahub 不能自行伪造
目的 ACK。以上状态都不是人类授权或端到端业务回执。

Live Relay reservation 只授权一个明确 owner/peer 组合；reservation ID 不是
bearer credential，双方 WebSocket upgrade 都必须用各自 descriptor key 签名并
消费持久 nonce。Relay 可见双方 Node ID、连接时间、帧大小和方向字节总量，但
只转发 binary bytes，不持有节点私钥，也不应终止未来的 Anet 端到端 TLS。每个
stream 受 reservation expiry、时长、双向字节、frame 和节点并发上限约束。
Ahub 重启会中止 live stream，但不会删除未过期 reservation。
节点级 Relay sync 通过临时、未广告的 loopback socket 在两端终止现有 TLS 1.3；
Ahub 只转发 TLS records。Relay path 仍执行固定 PeerCard、证书 fingerprint
channel binding 和双向签名握手，成功打开 WebSocket 本身不能取得可信 Agent
会话或工具能力。
启用 live Relay 时，owner 仅为配置中完整匹配的 peer 持续刷新 reservation；
discovery 以签名 caller 固定 `allowed_peer_id`，不能列举其他关系。节点对同一
Ahub/peer 的重复 owner 配置会失败关闭。运行时错误和路径指标只保留脱敏类别，
不记录 reservation ID、签名 path、Node ID 或正文。

参考服务默认只绑定 loopback，关闭包含路径的 Uvicorn access log、forwarded
header 信任、server banner 和 WebSocket compression；外部部署必须使用支持
authenticated WebSocket upgrade 的 TLS reverse proxy。完整可见元数据和运维
约束见 `docs/AHUB_V1.md`、`docs/RELAY_V1.md` 与
`docs/AHUB_OPERATIONS.md`。

## Companion 数据与授权边界

`anet.companion v1` 只允许 P0 低风险、端侧最小化后的 Observation/Episode。
节点收发门禁拒绝未知字段、原始音视频/图片、精确坐标、逐操作事件、通信正文、
健康明细、二进制 sensor blob 和 HumanState/诊断/情绪标签。Consent evidence
绑定 basis、grant ID 与具体 observation scope，但它只是可审计依据；Android
仍须在采集前检查系统权限和本地同意，并在撤销后停止采集和清除禁止上传的队列。

ApprovalDecision 必须逐字段匹配 ApprovalRequest 的 Human/Device、动作、
resource、参数摘要、scope、nonce 和有效期。该匹配只防止对象被移接或扩大，
不单独产生权限。高风险执行端现已把本地请求、可信 Decision claim、Packet
sender、当前未撤销的 `HumanDeviceGrant(approval.sign)`、request/nonce、
once/bounded 次数和 effect lease 写入持久账本；旧 worker token 不能结算新租约，
撤销会阻止后续 effect。MCP 执行入口默认关闭。

SQLite 账本不能与任意外部系统形成同一原子事务。每个 effect 会得到跨重启稳定的
`effect_idempotency_key`，真实 executor 必须把它传给下游并由下游去重；如果
下游忽略该键，进程在远端成功、本地 settle 前崩溃仍可能导致重复副作用。旋转的
execution token 只做本地 fencing，不构成无条件 exactly-once 保证。

## 能够防御

- 中继、网络服务商或普通旁路观察者读取消息正文；
- 中继伪造发送者或修改密文而不被发现；
- 单个消息服务器关闭导致所有排队消息消失；
- 重复传输导致同一对象多次进入 Inbox；
- 未固定身份的节点直接被当成可信 Agent；
- 节点短暂离线造成消息立即丢失；
- TLS 中间人替换证书后继续冒充已固定节点。
- 不可信目录服务读取或修改 Directory Carrier 的节点 ID、Packet ID、ACK 和密文对象；无效帧会被隔离。
- Directory/WebDAV v2 不再永久复用同一个方向邮箱 token；ACK 与常见小消息也进入相同的 4 KiB 起始长度桶。
- WebDAV 客户端不会探测尚不存在的派生邮箱，避免服务端从请求日志直接获得并关联整组历史/未来 token。
- 空闲直连和 Carrier 不再按严格固定短周期联网；随机抖动与有界退避减少机械心跳特征，新对象仍能绕过等待。
- 同一 Agent 消费组的并发 worker 通过 SQLite 原子租约避免同时执行一条消息；崩溃、NACK 和租约到期可以恢复，旧 token 不能完成新租约。
- MCP runtime 可把 owner、group/kind 前缀和允许 peer 固定为进程
  capability，并关闭绕过 claim 的 raw Inbox。
- Typed task 执行在写入幂等账本前同时要求显式入站 sender allowlist 与 capability policy；信任 Peer Card 本身不能取得执行权，模型参数不能添加授权项。
- v3 一次性预密钥在成功持久化后被逻辑擦除；取得长期静态 X25519 私钥本身不足以解开此前使用该预密钥截获的 Packet。
- 预密钥包由固定 Ed25519 身份签名；导入拒绝过期、代际回滚、同代分叉和预密钥 ID 重用，并发发送者不能取得同一远端预密钥。
- v2 预密钥签名和接收端私钥记录同时绑定唯一目标/发送 peer；第三节点取得公开 bundle 也不能消费该密钥。
- 自动补货在直连和 Directory-only 零库存条件下均已验证；重复 request ID 不重复生成 generation，异常超前 generation 被拒绝。
- 密文篡改、签名失败和预密钥作用域违规进入本地拒绝账本，不会在每次重启无限解密；暂时性 I/O 故障仍可恢复。
- 配对响应绑定随机 Offer、完整 Offer 摘要和有效期，不能把一次接受移接到另一轮配对；`pair-accept` 不暴露为 MCP 自动授权工具。
- 本地撤销 deny ledger 优先于残留 Peer Card；运行时握手和消息验信立即拒绝已撤销 Node ID，并以单事务终止其排队工作、未完成 claim 和 peer-scoped 密钥材料。
- SOCKS5 只改变直连 TCP 拨号路径；隧道建立后仍执行原 TLS 1.3、证书指纹通道绑定和双向签名挑战，代理不能成为 Anet 身份或信任来源。
- raw、SOCKS5 与 SOCKS5H 可作为多个独立拨号器并分别记录失败、冷却和 RTT；单条代理失效不会覆盖同一 locator 的其他路径指标。
- stdio adapter 只承载 TLS ciphertext；Anet 以 argv 直接启动绝对路径，不使用 shell，只复制最小平台环境和显式变量白名单，并在正常结束、故障、超时或竞速取消时回收进程。
- 有界 hedged sync 最多并发 1–4 个候选，后续候选延迟启动；首个完成端到端同步后取消落后连接，重复 Packet 由接收端幂等去重。
- `link-health-v1` 在固定身份握手后只交换 health frame，不携带业务 Packet；结果使用独立指标，旧节点在 capability 检查后安全退出。
- 有界跨 Carrier 复制可让一个 mailbox/账号/目录路径被删除或阻断时，另一条已取得相同密文的独立路径继续传递；接收端重复对象不会形成第二条 Inbox 或再次消费预密钥。

## 尚不能防御

- SOCKS5 代理可观察连接目标（`socks5h` 为域名，`socks5` 为本地解析后的 IP）、时间和流量大小，也可拒绝、延迟或篡改隧道；篡改会由 TLS/签名握手拒绝，但代理不可用仍会导致直连不可用。
- stdio adapter 同样可观察目标 locator、时序、长度和 TLS ciphertext，也能丢弃、重放或修改字节；TLS 与身份握手会拒绝篡改和冒充，但不能阻止拒绝服务。被列入 adapter 环境白名单的秘密属于该程序的权限，Anet 无法保护其免受恶意 adapter 读取。
- adapter 可执行文件和固定参数属于本机高权限配置。必须固定可信绝对路径、限制文件写权限并单独审查更新；不要让收到的消息、Peer Card、模型输出或远端输入决定 executable、argv 或环境变量名。
- 代理认证环境变量在进程环境和内存中可见。配置与 CLI 只保存/显示变量名，不等于系统密钥库；应限制进程环境读取权限并使用代理侧最小权限账号。
- 多拨号器会增加连接尝试、目标可见面和时序特征；恢复探测也可能被观察者利用。优先级、冷却和探测频率是可用性控制，不是流量分析防护。
- 竞速宽度越大、延迟越短，切换越快，但越容易产生同时连接、额外带宽和可关联时序。高敏感部署不应盲目设为 4；应以真实阻断率和观察面测量选择参数。
- 健康探针本身是主动、可观察的连接，会暴露测试时间、目标和拨号路径。错误分类可能受恶意代理或网络设备伪造，只能用于本地运维判断，不能证明审查者位置或攻击归因。
- Carrier 副本数越大，服务商可观察到的上传次数、账号暴露面、带宽和跨路径时序关联机会越多。复制提高可达性，不提供匿名性；应使用真正独立的服务、账号和网络原理，并以实测阻断收益选择 1–4，而不是无条件设为最大值。

- 全局观察者通过时间、长度桶和连接关系做流量关联；
- 目标 IP、端口或 TLS 流量被整体阻断；
- 已控制终端读取明文、私钥或 Agent 内存；
- 恶意可信节点拒绝转发、选择性丢包或泄漏邻接关系；
- Sybil 节点填满开放网络；v0 因此默认只连接手工固定的 Peer Card；
- 量子攻击；v0 尚未加入 ML-KEM 混合密钥建立；
- 群组成员动态加入、移除后的前向保密；v0 是逐接收者单播；
- 隐藏最终目的节点；v0 不是 onion/mix routing。
- 静态降级单播包和配对 Directory/WebDAV 帧仍可在长期 X25519 私钥泄漏后被历史解密；一次性预密钥只覆盖明确标记为 `key_mode=opk` 的 v3 Packet，尚无 Double Ratchet 或失陷后恢复。
- 已解密正文当前会持久化在本机 SQLite Inbox；控制终端、数据库、运行进程或 Agent 上下文的攻击者可直接读取它，不需要破解传输密文。
- 单次预密钥的 `private_key=NULL` 是 SQLite 当前逻辑状态，不是介质取证级安全擦除。备份、VM/文件系统快照、未及时截断的 WAL、SSD wear leveling 和内存副本可能保留旧材料。
- 尚未消费的预密钥私钥与长期身份一起存放在节点数据库/文件系统中；设备在使用前被控制时，攻击者可取得这些未来解密能力。
- 零库存恢复控制对象可使用静态 v3 信封。日后长期 X25519 私钥泄漏可能揭示补货时间、节点关系、目标 ID 和本来就是公开的预密钥；普通 Agent 内容不会通过该例外发送。
- 自动补货无法在身份数据库及 generation 基线同时丢失后安全猜测远端所见的最新代际；超前声明会被拒绝，需要从受信备份恢复或显式重新建立身份/信任。
- Directory Carrier 服务商删除、延迟、回滚文件，或通过时间、长度桶、同一账号和 epoch 内邮箱活动关联两端。
- 直接 Carrier 的相邻节点可见 QoS；攻击者可能据此优先干扰控制流量。高敏感部署应在 Carrier 层统一批次和长度，而不是依赖 QoS 保密。
- 路由指标只能证明本节点观察到的链路行为；恶意相邻节点仍可选择性 ACK、延迟或制造降级，不能把低 RTT 等同于可信。
- WebDAV 服务商或被盗账号可以枚举已经创建的不透明邮箱、观察时间和长度桶、删除或回滚对象，但没有节点私钥时不能解开 Anet 帧；HTTPS 主机本身仍可能按域名、SNI、IP 或账号被阻断。
- WebDAV 凭据允许操作邮箱对象，不等同于 Anet 身份密钥。仍应使用最小权限、独立账号和可轮换 token。
- v2 epoch 密钥仍由双方静态 X25519 配对秘密确定；终端密钥日后泄漏可重建历史/未来邮箱 token 和帧密钥。时间轮换降低长期公开标识，不提供密码学前向保密。
- 调度 jitter 不能隐藏真实消息触发的突发、总流量、在线时段或账号关系，也不能替代 cover traffic/mixnet；系统熵源或时钟受控时，调度随机性还可能退化。
- `trusted` 只验证固定节点密钥。MCP task sender/capability policy 限制可进入执行账本的范围，但可信 Agent 仍可能被提示注入、遭控制、犯错或在已授权 capability 内提交危险参数；worker 仍需参数、预算和具体副作用审批。
- 被控制的可信 peer 仍可制造补货请求和无效 Packet 消耗带宽、CPU 或磁盘。请求代际验证、30 秒响应去重、批量上限和拒绝账本限制放大，但不能消除已授权邻居的拒绝服务能力。
- typed task ledger 可防止相同 sender × group × task ID 被并发执行，并原子保存本地结果与 ACK；它不能原子覆盖任意外部系统。进程若在外部副作用成功后、本地 task settlement 前崩溃，任务仍可能接管重试；应把 task ID 传给下游作为幂等键或使用事务 outbox。
- claim token 会出现在 Agent 上下文和本地 MCP 流中。它只授权结算一条本地租约，应避免写入长期记忆、远端日志或回复；控制本机或模型上下文的攻击者仍可滥用当前 token。
- netem/network namespace 结果只覆盖指定延迟、抖动、随机丢包和单向黑洞模型，不模拟商业 DPI 的协议分类、主动探测、IP/域名联动封锁或法律层面的终端查控。
- `monitor` 会主动制造可观察流量，其 JSONL 还能揭示节点关系、在线时段、故障与切换路径。生产监测应降低频率、使用 jitter、限制文件权限并设置保留期；不能把监测当作 cover traffic。
- 签名配对只证明对方控制 Peer Card 对应私钥，不证明现实身份、设备未失陷或 NATS/文件发送者就是预期的人；敏感配对应在第二通道核对 Node ID。Offer/Response 是公开对象，不应承载秘密。
- v0.5.1 撤销只在执行节点本地生效，不会自动通知其他 peer，也没有离线根身份、设备子密钥、序列号或签名撤销证明。已执行的工具副作用、对方已取得的明文与其他节点的信任状态都不能回滚。

## 不使用“自制混淆即安全”

Anet v0 不伪装成 HTTP/2、视频或其他协议，因为不完整模拟会被主动探测轻易识别，并可能制造错误安全感。抗封锁演进应通过独立 Carrier 完成，并用真实故障和 DPI 环境验证：

- 多个协议原理不同的 Carrier；
- 可轮换 rendezvous 和无固定 bootstrap；
- Reticulum/Yggdrasil/cjdns 等覆盖地址；
- 可选 QUIC/WebTransport、合法 HTTPS mailbox 或 mixnet Carrier；
- 固定尺寸批次、延迟队列和 cover traffic；
- LAN、Bluetooth、LoRa 与人工携带的断网路径。

任何 Carrier 失效都不应改变 Node ID、Peer Card 或已封装对象。

## 私钥

`identity.json`、`tls-key.pem` 和 SQLite 中尚未消费的预密钥是高价值秘密。代码会尽力设置文件权限、启用 SQLite `secure_delete` 并在预密钥消费后请求 WAL truncate checkpoint，但 Windows ACL、备份软件、快照和存储介质仍可能复制它们。不要同步到公共云盘或提交 Git。正式节点应迁移到系统密钥库、TPM/TEE 或硬件令牌，并制定设备撤销、身份轮换、本地明文保留与安全销毁协议。
