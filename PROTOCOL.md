# Anet Object Protocol v0.11.0

## 1. 窄腰

Anet 的稳定对象不是会话或聊天记录，而是不可变的 `SealedPacket`：

> 名称与 wire format 分离：v0.4.0 的公开名称统一为 Anet，但 v1 域分隔标签不重写。修改这些字节会改变既有节点 ID、密钥派生结果和不透明邮箱，属于新协议版本而不是品牌改名。

```text
version
packet_id
destination_id
created_ms
expires_ms
max_hops
qos
key_mode
prekey_id
ephemeral_x25519_public_key
nonce
ciphertext
```

外层字段作为 AEAD additional authenticated data，因此中继不能静默修改目的节点、时间、有效期、跳数上限、QoS、密钥模式、预密钥 ID 或临时公钥。实际中继深度属于每条 Carrier 路径的本地状态，不写入不可变对象。解析器仍接受没有 QoS 字段的 v1 对象，以及没有密钥模式字段的 v2 静态对象；v3 才允许 `key_mode=opk`。

## 2. 内层对象

解密后得到：

```text
version
sender_node_id
sender_ed25519_public_key
sender_x25519_public_key
kind
created_ms
body
causal_packet_ids[]
codec
reply_to
signature
```

发送者 Node ID 由两把公钥共同派生。签名覆盖除签名自身外的所有内层字段。接收方先验证 Node ID，再验证签名，最后与本地固定的 Peer Card 比较决定 `trusted` 或 `untrusted`。

### 2.1 显式配对

`PairOffer v1` 是传输无关的公开对象，包含随机 128-bit `offer_id`、发起端签名 Peer Card、创建时间、过期时间和发起端签名。有效期默认一小时、最长七天；验证端允许有限时钟偏差但拒绝已经过期或来自过远未来的对象。

接受端只有在本地明确执行 `pair-accept` 后才固定 Offer 中的 Peer Card。随后生成 `PairResponse v1`，其中包含接受端签名 Peer Card、原 `offer_id`、完整 Offer 的 SHA-256 摘要、接受时间和接受端签名。发起端执行 `pair-complete` 时必须同时提供自己签发的原 Offer；响应不能移接到另一份 Offer，也不能用来完成自配对。

Offer/Response 可以通过 NATS、目录、二维码、U 盘或人工携带交换。传输通道只负责搬运对象，不决定信任。该流程证明两端分别控制声明的 Ed25519 私钥并同意本轮挑战，但不证明设备操作者的现实身份；高风险部署仍应通过第二通道核对 Node ID。

### 2.2 本地撤销

`revocations.json v1` 是节点本地、私密、追加语义的 deny ledger。记录包含完整 Node ID、撤销时间、有限长度原因和当时固定公钥的 SHA-256 指纹。撤销时先原子持久化 deny 记录，再删除正向 Peer Card；即使两次写入之间崩溃，加载器也必须让 deny 记录优先于残留的正向记录。

信任边界在出站队列、入站握手及解密后身份核对时重读该 ledger。撤销后的同一 Node ID 不能通过普通 Card 导入或 PairResponse 恢复。SQLite 清理作为单一事务使该 peer 的未到期待发 Packet 过期、历史 Inbox 变为 untrusted、未完成 consumer delivery 进入 `revoked`、双方 peer-scoped 预密钥退出可用状态，并删除路径指标和选路状态；已确认的历史 delivery 与审计元数据保留。

这是每个节点独立执行的本地策略，不是签名的网络级撤销声明，也不传播到其他节点。重新建立关系应轮换为新的密码学身份；后续协议仍需定义离线根身份、设备子密钥、序列号与可传播撤销证明。

### 2.3 控制平面身份与可达性分离

PeerCard v1 继续作为已部署信任和 Packet 加密对象，其签名域、Node ID 推导和
wire format 不变。新的并行控制平面使用无地址 `NodeDescriptor v2`、短期
`ReachabilityRecord v1`，以及独立人类主体对设备 Node ID 的授权/撤销链。

这些对象不自动扩大 PeerBook 信任，也不允许目录代表节点修改地址或能力。对象
字段、有效期、修订链、重放/分叉规则和当前未实现边界见
[`docs/CONTROL_PLANE_V1.md`](docs/CONTROL_PLANE_V1.md)。

### 2.4 Ahub Rendezvous 与 Mailbox

P0.2 Ahub 只接受 operator allowlist 内的完整 Node ID。Descriptor 和
Reachability 使用自身签名/修订链；查询、上传、领取和 settle 使用绑定
method、path、body digest、时间窗和持久单次 nonce 的 `AhubRequest v1`。

Mailbox 原样保存已由目标节点密钥端到端加密的 `SealedPacket`。上传成功只表示
Ahub custody；领取不会删除，目标节点完成本地持久化后才以短期 claim token
settle。settle 必须携带目标节点签名的 `DestinationSettlement v1`，绑定 Packet
ID、原始密文 SHA-256、上传者、目标和有效期。发送端从 Ahub 取回证明后，以
已固定目标签名公钥和本地 Packet 复核，服务器不能自行伪造目的 ACK。

业务 receipt 仍是普通 Anet Packet，本地 consumer ACK 仍属于执行边界；它们
不能由 Ahub custody/settlement 状态合成。完整边界见
[`docs/AHUB_V1.md`](docs/AHUB_V1.md)。

### 2.5 Companion 双向对象

P0 主力手机使用六个严格的 `anet.companion v1` 对象：

```text
companion.observation.batch
companion.episode
companion.intervention
companion.user-response
companion.approval.request
companion.approval.decision
```

Packet kind 必须与 `object_type` 精确匹配，未知字段和未定义的
`companion.*` kind 失败关闭。节点在封包前与解密落盘前都执行完整校验。P0
Observation 只允许电量、粗粒度网络、低频 presence、用户主动 Self Report 和
逐项授权的应用类别时间窗；原始音视频、精确坐标、逐操作事件、通信正文、健康
明细和 `HumanState`/诊断/情绪标签不能进入对象。

Approval 绑定 Human ID、明确 Device Node ID、capability、resource、参数摘要、
scope、风险、短 TTL 和 nonce；Decision 必须逐字段重复这些安全相关值。对象
校验不替代外层 Packet 签名、`HumanDeviceGrant(approval.sign)`、撤销检查和执行
端的原子 nonce/副作用账本。完整字段与 JSON 互操作 fixtures 见
[`docs/COMPANION_V1.md`](docs/COMPANION_V1.md)。

## 3. 加密

每个包都生成新的发送端 X25519 临时密钥。v3 支持两种受认证模式：

- `static`：与接收者 Peer Card 中的长期 X25519 公钥进行 ECDH；
- `opk`：与接收者签名发布、发送端原子预留的一次性 X25519 公钥进行 ECDH。

共享秘密通过 HKDF-SHA256 派生 ChaCha20-Poly1305 密钥。v3 KDF 绑定 `packet_id`、接收者实际加密公钥、临时公钥、`key_mode` 和 `prekey_id`。v1/v2 保留原有 KDF，便于滚动升级。发送方只有在固定 Peer Card 声明 `one-time-prekeys-v1` 时才发送 v3；旧节点继续收到 v2 静态包。

预密钥包 v2 由节点 Ed25519 身份签名，包含发布节点、唯一 `intended_peer_id`、按该 peer 独立递增的 generation、创建/过期时间以及 1–1000 个由公钥派生 ID 的 X25519 公钥。导入端验证固定 Peer Card、本地目标、签名、过期时间、generation 回滚和同代不同内容；同一远端预密钥通过 SQLite `BEGIN IMMEDIATE` 只会被一个并发发送者预留。预留后即使封包失败也只会 burn，不会回池重用。

接收端数据库把每个私有预密钥绑定到获授权发送 peer。解密内层签名身份后必须再次匹配该作用域；其他节点即使取得公开密钥并能形成有效 ECDH，也不能消费它。Peer Card 声明 v2 时发送端只选择 v2 库存；只声明 v1 的滚动升级节点仍可使用旧包。

接收方先读取仍可用的私钥并解密；随后在同一 SQLite 事务中写入 Inbox、标记 Packet 已交付、把该私钥设为 `NULL` 并绑定 Packet ID。事务前崩溃会保留密钥供 spool 重试；事务后重放相同 Packet 由 Packet/Inbox 主键直接确认，不需已擦除密钥；不同 Packet 重用同一预密钥会被拒绝。

这是异步单播传输密文的有限前向保密，不是 Double Ratchet。它不覆盖静态降级包、尚未消费的预密钥、Directory/WebDAV 静态配对帧、接收端已持久化的明文 Inbox、备份/快照/SSD 历史页或运行中内存，也没有失陷后恢复。

### 自动补货

每个节点监测“对某 peer 可用于发送的 v2 公钥”而非全局库存。低于 low watermark 时生成确定性 `request_id`，发送：

```text
network.prekey.request
  version
  request_id
  known_generation
  observed_available
  requested_count
```

发布者若已有比 `known_generation` 更新且未过期的本地 bundle，重发相同签名 bundle；否则生成下一 generation。声称高于发布者本地 generation 的请求被拒绝，30 秒内重复 request ID 不重复生成或排队响应。响应为：

```text
network.prekey.bundle
  version
  request_id
  signed_peer_scoped_bundle
```

这两种 kind 由运行时保留、隐藏于普通 Inbox，并且不产生应用 receipt；Carrier custody ACK 负责停止重传。bundle 在控制 Packet 提交前先幂等导入，因此“导入后崩溃”可安全重放。普通公共 `queue/send/MCP` 不能构造保留 kind。

控制对象优先使用 v2 opk。库存为零时允许它们使用 v3 static 信封作为恢复通道；其中只有请求和签名公钥，且普通 Agent 消息的 `require` 策略不受影响。这样补货仍可经 TLS、Directory、WebDAV 或离线 Bundle 完成，不要求额外中心服务。

v0.3.7 的未绑定库存只有在节点恰有一个固定 peer 时才会自动迁移；多个 peer 时必须逻辑擦除共享私钥并为每个 peer 从旧最大 generation 之后重新签发，避免把同一私钥错误分配给多个关系。

发送者身份只存在于密文中，因此普通中继无法从 Anet 对象判断原始发送者。接收者 ID 在 v0 外层可见，以支持小规模 mesh 转发。

## 4. 长度填充

内层数据以 4 字节真实长度开头，随后填充到不小于配置下限的 2 的幂次桶。填充使用密码学随机字节。v0 隐藏精确长度，但不隐藏长度桶。

## 5. 链路认证

TLS 1.3 仅作为当前 Carrier。Anet 不依赖 Web PKI：

1. 服务端发送随机挑战、Peer Card 和 TLS 证书指纹，并签名；
2. 客户端核对固定公钥及实际 TLS 指纹；
3. 客户端签名服务端随机数、客户端随机数和指纹；
4. 服务端核对固定公钥并返回最终签名。

这把自持节点身份绑定到当前 TLS 通道，并通过服务端随机挑战阻止简单重放。

可选 SOCKS5/SOCKS5H 发生在 TLS 之前，只建立到 Peer Card 既有 `tls://` 地址的 CONNECT 字节流。`socks5` 由客户端解析域名并发送 IPv4/IPv6 地址，`socks5h` 把域名交给代理；两者均严格验证方法、可选 RFC1929 认证和 CONNECT 回复。代理配置、协商结果和凭据不进入 Peer Card、签名挑战、`SealedPacket` 或任何 Carrier 帧，因此不会改变 Node ID、wire format、信任或撤销语义。

`stdio` 拨号器同样发生在 TLS 之前。Anet 使用绝对可执行文件路径和固定 argv 直接启动外部程序，不调用 shell；程序从 `ANET_TARGET_HOST`、`ANET_TARGET_PORT` 取得目标，把 stdin/stdout 映射为可靠、有序、双向字节流。该程序看到的是 TLS ciphertext，不取得 Anet 私钥、PeerBook、Packet 数据库或明文。只有显式列入配置的环境变量才会从父进程复制。TLS 成功后继续执行完全相同的 5 节身份握手，因此外部 adapter 不能自行声称远端身份。adapter 合约和生命周期见 `docs/STDIO_DIALER.md`。

### 5.1 作用域定位器

Peer Card v1 继续把地址保存为签名字符串，并允许以下向后兼容的查询参数：

```text
tls://127.0.0.1:4242?scope=host&zone=<opaque>&priority=0
tls://192.0.2.10:4242?scope=lan&zone=<opaque>&priority=20
tls://example.invalid:4242?scope=wan&priority=100
```

`scope` 只能是 `host`、`lan` 或 `wan`。`host`/`lan` 必须带 8–64 字符的不透明 `zone`；本地配置只有持有相同 `host:<zone>` 或 `lan:<zone>` context 才尝试该地址。较小 `priority` 先尝试。没有查询参数的旧地址按 `legacy` 处理，仍可被所有节点尝试。未知、重复或畸形参数 fail closed。

作用域和 zone 只是可达性提示，不是秘密、身份、授权或信任证明。Node ID 与双向签名握手仍是唯一节点认证边界。zone 应使用随机不透明值，避免写入设备名、地点或组织名；Peer Card 的签名防止中途修改提示，但不保证提示真实。旧版节点会忽略查询参数并尝试地址，因此滚动升级保持可连接，但只有新版节点会在拨号前执行作用域过滤。

### 5.2 多拨号器路径

拨号器是本地配置，不进入 Peer Card。每个启用的 raw/SOCKS5/SOCKS5H/stdio 拨号器与每个可用 locator 形成独立候选路径：

```text
direct:<dialer-name>:<signed-locator>
```

节点为候选分别记录 EWMA RTT、累计成功/失败、连续失败和最后错误。排序首先避开仍在失败冷却期的候选，再使用 locator priority 与 dialer priority 的和，最后使用失败数和已知 RTT。一次候选失败不会改变 Node ID、密文或队列；同轮继续尝试下一候选。连续失败达到 direct threshold 后，候选在 `direct_retry_interval` 内降级；到期后自动重新参与高优先级选择，以检测路径恢复。

顶层 `direct` 指标和 Carrier 路由仍表示“任一直连拨号路径”的聚合健康度；细粒度 ID 用于拨号器内部选择和归因。传递 ACK 仍记为 `direct`，避免把同一密文因拨号实现差异误认为不同语义 Carrier。

### 5.3 有界候选预热

`direct_race_width` 控制每批最多并行的直连候选，范围为 1–4；`direct_race_delay` 控制后续候选相对首选路径的延迟启动，范围为 0–5 秒。宽度 1 保持顺序拨号。宽度大于 1 时，同批候选按 5.2 的确定性顺序启动，首个完成完整 TLS、签名握手、双向同步和 ACK 流程的候选获胜；尚未完成的任务被取消并关闭连接。只有同批全部失败才进入下一批。

并发候选可能携带相同 `SealedPacket`。接收端必须继续依赖 Packet ID、SQLite 唯一约束和幂等 ACK 去重，不能把 transport race 解释为两个业务动作。已完成的重复同步可能增加路径 attempt，但不得生成第二条 Inbox 或重复消费一次性预密钥。取消不记为路径失败，避免本地调度选择污染真实健康指标。

### 5.4 认证链路健康

支持方在签名 Peer Card 中声明 `link-health-v1`。客户端完成 TLS 与 5 节双向签名握手后发送 `{t: health}`，服务端仅返回 `{t: health-reply}` 并关闭连接。该交换不读取或推送 Pending Packet，不生成网络 Probe、receipt 或 Inbox，也不改变业务 delivery path。客户端在握手返回的实时签名 Card 中确认 capability；对旧节点返回 `health_unsupported`，不发送未知 frame。

健康结果写入本地 `health:<dialer-name>:<signed-locator>` 指标，与 `direct:*` 业务同步指标隔离。类别至少区分 `proxy_config`、`proxy_auth`、`proxy_unreachable`、`proxy_timeout`、`proxy_target_rejected`、`target_dns`、`tcp_refused`、`tcp_unreachable`、`tcp_timeout`、`tls_handshake`、`identity_handshake`、`health_unsupported` 与 `health_protocol`。分类是本节点观察结果，不是远端可验证事实。

## 6. 传递语义

- 中继节点 ACK 只表示“已验证外层并取得密文保管权”，不会把对象标为全局送达。
- 来自对象最终目的节点的 ACK 表示该节点已接受对象；端到端 `receipt` 另外提供可审计的应用层确认。
- 最终接收节点解密并验证可信发送者后，自动生成端到端 `receipt`。
- Packet ID、SQLite 主键和 Inbox 主键共同提供 at-least-once 传递下的幂等去重。
- 密文、签名、预密钥作用域等确定性失败写入本地 `packet_rejections` 并终止 spool 重试；SQLite/OSError 等暂时故障不进入该账本。
- 包在 TTL 到期或达到 `max_hops` 后停止传播。
- 本地 `carrier_replica_count` 可将同一不可变 Packet 有界复制到 1–4 条健康异步 Carrier。它不进入 Peer Card 或 wire format；默认 1 保持旧行为。任一路 custody ACK 都可停止发送端继续投递，其他路径稍后到达的副本由 Packet ID、SQLite 主键和 Inbox 主键幂等确认。
- `mode=always` 是管理员对特定 Carrier 的显式常发策略，不受自动副本预算削减。副本选择只改变本地搬运路径，不改变 QoS、密文、签名、TTL、预密钥消费或最终 receipt 语义。
- v0 对每个可信相邻节点发送未确认的密文，通过去重形成小规模受控泛洪。

## 7. Carrier 独立性

TLS sync、stdio 承载的 TLS sync、Directory Carrier 和离线 Bundle 都只搬运同一个 `SealedPacket`。外部字节链路可先通过 stdio adapter 接入；需要原生异步存储转发语义时再实现独立 Carrier。后续可增加 Reticulum、libp2p、QUIC、Waku、Bluetooth、LoRa、共享内存或 SDR Carrier，而不改变对象加密和身份语义。

### Directory Carrier v2

Directory Carrier 使用双方静态 X25519 密钥派生共享秘密，再按发送方向和绝对时间 epoch 分别通过 HKDF-SHA256 派生：

- 不透明邮箱 token；
- ChaCha20-Poly1305 投递帧密钥；
- 不透明文件名 HMAC 密钥。

v2 的 epoch 为 7 天。发送方只写当前 epoch；接收方计算当前、下一时钟偏差窗口、覆盖 31 天保留期的历史 epoch，以及 v1 兼容邮箱。不同 epoch 的 token、帧密钥和命名密钥互不相同。v2 collection 直接使用 27 字符不透明 token，不再使用 v1 的 `ch-` 特征前缀；对象名为 48 位 HMAC 截断值且没有固定扩展名。

投递帧分为 `packet` 与 `ack`，包含版本、epoch、发送者、接收者、时间、随机 salt、Packet ID、深度和可选密文对象。整个内层由发送节点 Ed25519 签名，再填充到至少 4096 字节及后续 2 的幂次桶，最后由方向密钥加密；共享目录无法伪造 ACK、修改对象或从常见小帧大小直接区分 ACK 与消息。接收方先验证 AEAD、epoch/时间一致性、固定公钥签名、方向、文件名与对象元数据，再交给统一 `SealedPacket` 验证路径。

发送版本由对方签名 Peer Card 协商：声明 `directory-carrier-v2` 才发送 v2，否则继续发送 v1；v2 接收端始终保留 v1 解码。因此升级顺序不会使旧节点失联，但只有重新交换新 Card 后才会启用轮换邮箱。

该 Carrier 不打开监听端口，也不信任目录复制服务。轮换和长度桶不能隐藏文件出现时间、账号级活动、epoch 内的同一邮箱活动或复制服务本身的可达性；静态 X25519 配对密钥泄漏后仍可重建各 epoch，因此这不是前向保密 ratchet。

### WebDAV Carrier v2

WebDAV Carrier 复用同一配对信道帧，但将不透明信道映射到 WebDAV collection，将帧映射到不可覆盖创建的资源：

```text
PROPFIND <base>/ Depth: 1
MKCOL   <rotating-token>/
PUT     <rotating-token>/<hmac-name>  If-None-Match: *
PROPFIND <existing-token>/ / GET
DELETE  已取得保管权的帧
MOVE    无效帧到 quarantine（不支持时删除）
```

客户端先列出基础 collection，只访问“服务器已存在且本地能推导验证”的邮箱，不会主动逐个探测未出现的历史或未来 token。这样既减少请求，也避免 WebDAV 请求日志直接得到整组轮换 token。节点只发出 HTTPS 请求，不要求 WebDAV 服务理解 Anet。认证 token、用户名和密码从本地环境变量读取，不进入配置的值字段、Peer Card 或 `SealedPacket`。实现拒绝远端明文 HTTP、URL 内嵌凭据和 HTTP 重定向。

WebDAV 发送版本由 `webdav-carrier-v2` 能力独立协商。v1 的 `ch-<token>/<name>.drop` 可继续枚举和读取。

## 8. QoS 与弹性路由

v2 `SealedPacket` 定义四种受认证 QoS：

| QoS | 典型用途 | 故障行为 |
|---|---|---|
| `control` | 取消、授权、回执 | 第一次失败即可竞速备用路径 |
| `interactive` | 人与 Agent 的交互 | 第一次失败即可竞速备用路径 |
| `normal` | 普通任务、事件和结果 | 达到连续失败阈值后切换 |
| `bulk` | 大对象和低优先级同步 | 排在控制与普通消息之后 |

每个节点在本地持久化 `peer × path` 指标：尝试、成功、失败、连续成功/失败、成功 RTT 的 EWMA、最后错误和时间。路由只决定承载方式，不改变 `SealedPacket`。

状态机采用 fast-fail/slow-recover：

```text
DIRECT
  ├─ 单次失败：control/interactive 可在备用路径竞速
  └─ N 次连续失败：FALLBACK

FALLBACK
  └─ M 次连续成功 + cooldown：DIRECT
```

备用期间仍探测直连。接收端的 Packet ID 去重和逐路径 custody ACK 使重复竞速保持幂等。

存在多条备用 Carrier 时，路由先排除达到连续失败阈值的路径，再按显式 priority、成功 RTT EWMA 和稳定性选择。当前备用路径失败会切到健康的下一条；更高优先级路径连续恢复达到阈值且冷却结束后才切回。

选中备用路径后，直连恢复探测作为低频后台任务运行，不阻塞 Carrier 数据面。主动 probe 检测到 Carrier 帧已在途时，只轮询对应 Carrier 拉取 receipt，不会立即重复一个可能超时的直连探测。

`delivery_paths` 以 `packet_id × peer_id × path_id` 记录尝试和 ACK。主动 `network.probe` 等待端到端 receipt，并返回真实确认路径；probe 和 receipt 作为 transient Inbox 对象保存去重证据，但默认不展示给 Agent。

## 9. 后台调度

常驻节点把“本地检查频率”和“实际联网频率”分开。SQLite 队列仍以短 tick 检查，但没有新对象时不会每个 tick 都建立直连或访问远端 Carrier。

每个 `peer × direct` 与每个 Carrier 分别维护内存调度状态：

```text
new unseen Packet  -> bypass next_due and run now
activity           -> reset backoff
idle/failure       -> 1x, 2x, 4x ... bounded backoff
every delay        -> random factor in [1-jitter, 1+jitter]
```

默认值：直连失败重试 5 秒，空闲健康探测 60 秒、±35% jitter、最大 4 倍退避；Directory/WebDAV 使用各自基础 interval、±25% jitter、最大 4 倍空闲退避。fallback 恢复探测也使用独立 ±35% jitter，并在连续失败时退避。全局本地 tick 自身使用 ±20% jitter，避免所有路径被固定时间栅格量化。

“新 Packet”以当前待发 Packet ID 集合相对上次观察的增量判断，因此同一个未确认对象不会在每个本地 tick 都绕过退避；新的控制对象仍能立即唤醒。显式 `sync`、`probe`、`benchmark` 属于调用者要求的强制执行，不受后台 `next_due` 限制。

调度状态不是协议共识，也不进入 `SealedPacket`。进程重启会重置它并立即做一次发现；这有利于恢复，但重启本身仍会产生可观察流量。随机抖动只减少机械周期特征，不隐藏真实活动时间，也不构成 cover traffic。

## 10. 本地 Agent 消费协议

网络层的端到端 receipt 表示目的节点已经验证并持久保存对象，不表示某个 Agent 已经完成任务。v0.3.6 在 Inbox 上增加独立的 durable consumer group：

```text
network receipt
    -> durable Inbox
        -> consumer group delivery
            pending/absent -> leased -> acked
                           -> retry
                           -> lease expired -> leased by another worker
```

消费组包含不可变过滤条件：`start_after_rowid`、kind prefix、可选 sender Node ID、trusted-only 和是否包含 transient。`latest` 在创建时记录当前最大 Inbox rowid，避免毫秒时间相同造成遗漏；`earliest` 仅用于明确的历史重放。

`claim(group, owner)` 使用 SQLite `BEGIN IMMEDIATE` 原子选择消息，并生成 128-bit 随机 claim token。相同 group 的并发 worker 不会同时取得同一 Packet；不同 group 是独立 fan-out。租约最长 24 小时，可续租；租约过期或 NACK 延迟结束后，下一 worker 获得新 token，旧 token 的 ACK/NACK/renew 均失败。ACK 应只发生在 Agent 的输出和副作用已经可靠提交之后。

消费状态不改变 `SealedPacket`、网络 ACK、端到端 receipt 或传统人工 `is_read` 标志。消费组因此可在不增加远端元数据的情况下升级或重建。过期 transient Inbox 被清除时，其孤立 consumer delivery 同步清理。

MCP 适配器可用环境 capability 将 owner、group 前缀、kind 前缀和允许 peer 固定在进程边界；模型传入的参数只能进一步缩小，不能扩大。`trusted=true` 只表示发送者密钥匹配固定 Peer Card，不表示载荷可以绕过本地工具权限与副作用审批。
