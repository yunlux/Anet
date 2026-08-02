# Anet v0.12.1

[English](README.md) | **简体中文**

[官网](https://anet-network.yunluxyz.chatgpt.site/) ·
[文档](docs/) ·
[协议](PROTOCOL.md) ·
[安全说明](SECURITY.md) ·
[参与贡献](CONTRIBUTING.md)

v0.12.1 现在以“一键部署”为新设备的默认路径：它在 Windows、WSL、Linux、macOS
和 Android Termux 上创建一个独立节点，安装 Anet runtime，启动服务端/客户端进程，
并把控制页地址保存到节点 home。运行中的 supervisor 会持续读取远程 JSON 控制页，
按页面声明更新软件、默认配置、Peer Card，以及嵌套的 pages/kv 数据源。

## 新设备一条命令安装

控制页至少需要提供 `software.version`，以及首次安装用的
`software.wheel_url` 或 `software.repo_url`（顶层 `repo_url` 也可以）。
wheel 加 `software.sha256` 是可重复的安装路径；如果只提供仓库地址，
安装器会让 pip 通过 Git 安装，因此设备需要有 Git。相同的 `repo_url` 还用于
远程入口下载匹配的辅助脚本，以及 supervisor 后续的源码更新。
可选的 `software.repo_ref`（或顶层 `repo_ref`）可以固定 Git 分支、tag 或 commit；
辅助脚本、首次安装和后续源码更新会使用同一个引用。
页面格式见 [控制页示例](docs/windows-control-page.example.json) 和
[Windows 自动启动文档](docs/WINDOWS_AUTOSTART.md)。

Windows 普通用户在 PowerShell 中直接执行这一条命令：

~~~powershell
& ([scriptblock]::Create((Invoke-RestMethod https://raw.githubusercontent.com/yunlux/Anet/main/scripts/install_windows_oneclick.ps1))) -ControlUrl https://example.invalid/anet/control.json
~~~

如果需要整机启动、无需用户登录，请在“以管理员身份运行”的 PowerShell 中加入
`-Admin`：

~~~powershell
& ([scriptblock]::Create((Invoke-RestMethod https://raw.githubusercontent.com/yunlux/Anet/main/scripts/install_windows_oneclick.ps1))) -Admin -ControlUrl https://example.invalid/anet/control.json
~~~

普通用户模式使用 `%LOCALAPPDATA%\Anet` 和 `AtLogOn` 计划任务；
管理员模式使用 `%ProgramData%\Anet`、`SYSTEM` 账户和
`AtStartup` 计划任务。两种模式都会先执行有界的只读重复检测：发现已存在的
Anet/Ahub runtime、服务、任务或进程时停止，不会静默创建第二套部署。明确需要第二套时才使用
`-AllowExisting`。

其他平台使用同一控制页和部署模型。以下入口需要在 Anet checkout 中运行（上面的
Windows PowerShell 命令不需要先 checkout）：

~~~bash
# WSL
python3 scripts/install_wsl_oneclick.py --control-url <CONTROL_URL>

# 非 WSL Linux
python3 scripts/install_linux_oneclick.py --control-url <CONTROL_URL>

# macOS
python3 scripts/install_macos_oneclick.py --control-url <CONTROL_URL>

# Android Termux
python3 scripts/install_termux_oneclick.py --control-url <CONTROL_URL>
~~~

WSL 和 Linux 使用当前用户的 `systemd --user`，macOS 加载 `LaunchAgent`，
Termux 使用 `termux-services`/runit 和 Termux:Boot。若要求 WSL 在 Windows
重启后也自动拉起，额外在 Windows PowerShell 执行：

~~~powershell
.\scripts\register_wsl_keepalive.ps1 -Distribution Ubuntu -LinuxUser <LINUX_USER>
~~~

Windows 与 WSL 即使使用镜像网络仍是两个独立节点。两端必须使用不同监听端口和相同的
`host:<zone>` 上下文；端口只能隔离监听冲突，不能让两边的
`127.0.0.1` 互相等价。host-scoped 直连必须使用两边都能到达的非回环地址，
或监听 `0.0.0.0` 并显式设置 `-Advertise`/`--advertise`。

如果只想安装 runtime、不创建持久节点或服务，仍可使用纯净安装入口：

~~~text
Windows  scripts/install_windows.ps1
WSL      scripts/install_wsl.py
macOS    scripts/install_macos.py
Linux    skills/install-anet/scripts/install.py
~~~

## 让 Agent 辅助安装

如果希望由 Codex 或其他具备代码执行能力的 Agent 执行部署，可复制下面的提示词。
它应优先调用上面的一键部署入口，最后报告节点 ID、node home、服务/任务状态和控制页地址，
且不得复制其他设备的 identity、TLS 私钥、SQLite 状态或整个 node home：

~~~text
从 https://github.com/yunlux/Anet 安装并部署 Anet。使用这个控制页 URL：
<CONTROL_URL>（执行前替换占位符）。控制页必须提供 `software.version` 以及
`software.wheel_url` 或 `software.repo_url`；使用仓库地址时，可选的 `repo_ref` 可以固定
Git 分支、tag 或 commit。本请求已授权创建一个独立持久节点、服务/自启动
以及 supervisor 对远程控制页的轮询。自动检测平台并调用对应的 one-click 入口；
原生 Windows 使用 PowerShell 一条命令，只有需要整机启动时才加入 -Admin；WSL、Linux、
macOS、Termux 使用 checkout 中对应的一键脚本。如果 WSL 还必须在 Windows 重启后恢复，
且主机侧操作已获授权，再注册 WSL keepalive 任务。Windows 与 WSL 必须视为两个节点：
使用不同 node home、identity、Node ID 和监听端口，不能把 127.0.0.1 作为 host-scoped
peer 地址发布。最后报告 runtime、独立节点、服务/任务状态、节点 ID、node home 和控制页地址。
遇到已有部署、身份、哈希、权限或控制页格式冲突时停止；只有明确要求第二套部署时才使用
-AllowExisting/--allow-existing。禁止从其他设备复制 identity、TLS 私钥、SQLite 状态或整个
node home。
~~~

这段提示词只是把上面的命令交给 Agent 执行，不改变各平台的 node home、身份隔离和重复检测规则。

v0.5.2 为 TLS 直连增加可选的严格 SOCKS5/SOCKS5H 拨号。代理只替换到 peer 的 TCP 建链方式；Node ID、Peer Card、TLS 证书通道绑定、双向签名挑战、密文对象、信任和撤销语义均不改变。

v0.6.0 起支持签名的作用域定位器，使共享镜像 IP 的 Windows/WSL 与独立 Mac 不再把回环地址、同机地址和 LAN 地址混为一谈。作用域只筛选路径；身份仍由 Node ID 和握手签名决定。v0.7.0 再把 raw 与各 SOCKS 代理拆成独立拨号器路径。

v0.7.0 把 raw、SOCKS5 和 SOCKS5H 变成可并存、可独立计分的拨号路径。高优先级拨号器连续失败后进入短暂冷却，节点在同一次同步中尝试其他拨号器；冷却结束后自动恢复探测，不需要人工切换全局代理开关。

v0.8.0 增加有界 hedged sync：首选候选在短暂窗口内没有完成时，节点才预热下一条已经认证的直连候选；第一条完整同步成功后取消落后连接。默认宽度为 1，保持旧流量行为。受限网络节点可显式设为 2–4，并控制启动间隔：

v0.9.0 增加不携带业务 Packet 的认证拨号器健康探针。它区分代理不可达、认证失败、目标拒绝、DNS、TLS、Anet 身份握手和 health frame 故障，并把结果写入独立 `health:*` 指标，不污染正常路由成绩、Inbox 或 receipt：

v0.10.0 增加有界跨 Carrier 复制。默认仍只使用一个异步 Carrier；受限网络节点可把副本数设为 2–4，使同一不可变密文同时进入多个独立 mailbox/目录路径。任一路 ACK 都会使发送端收敛，接收端按 Packet ID 去重，不产生第二条 Inbox：

v0.11.0 增加无 shell 的 `stdio` 直连拨号器。外部程序只负责把 stdin/stdout 搬到任意可靠双向字节链路；Anet 在该字节流内部继续建立 TLS 1.3、固定证书指纹、双向签名身份握手、Packet 同步和 ACK。这样串口、无线电 modem、SSH 管道、自定义混淆链路或未来物理层实验可以独立开发，不接触节点私钥，也不改 Anet wire format。完整适配契约见 [`docs/STDIO_DIALER.md`](docs/STDIO_DIALER.md)。

Hermes 实时收件采用独立、固定、版本化的 Anet session；本地 wake bridge 只发送不含正文的 edge hint，Hermes Anet platform 直接领取 durable batch，并在一次 Agent turn 正常结束后 ACK。它不把 Anet 事件插入 Discord，也不依赖 MCP 完成入站 claim/settle。配置、失败语义与缓存测量见 [`docs/HERMES_WAKE.md`](docs/HERMES_WAKE.md)。

Agent 任务可使用严格的 `agent.task.request/status/result/cancel` 信封和单一
`anet_task` MCP 工具。它提供稳定 task ID、生命周期校验和 capability 声明，
但不把发送者身份误当作本地操作授权。协议与 A2A/EigenFlux 网关边界见
[`docs/AGENT_TASK_PROTOCOL.md`](docs/AGENT_TASK_PROTOCOL.md)。A2A 1.0 的首个
纯映射层已覆盖标准 Agent Card、sender-scoped `messageId` 幂等转换，以及
status/artifact 事件映射。SQLite 还固定认证 principal → Anet Node ID，并
持久保存 A2A task/context/message → 内部 task 的多轮关系。外部 message
注册会在同一事务创建 dispatch intent；本地租约 worker 原子提交 Packet、
一次性预密钥使用和 outbox 结果，进程崩溃后可以续派。A2A CancelTask 会向
该外部 task 下所有内部 task 持久派发取消；接收端支持乱序 tombstone、协作停止
和 execution-token fencing，不会把“已发出取消”误报为“已经停止”。它尚不开放 HTTP 入口，
边界和后续门禁见
[`docs/A2A_V1_GATEWAY.md`](docs/A2A_V1_GATEWAY.md)。

EigenFlux 与 Matrix 分别作为发现面和同步状态机的设计参考：Anet 后续只吸收
public-safe 的声明式信号匹配，以及 event/state、游标、gap/backfill 和逐设备
撤销原则；不把中心 matcher 变成授权者，也不在当前窄腰引入聊天 room 或完整
homeserver 联邦。决策记录见
[`docs/EIGENFLUX_MATRIX_REFERENCE.md`](docs/EIGENFLUX_MATRIX_REFERENCE.md)。

Abazr（简称 ABA）是位于 Anet 之上的独立 Agent Bazaar 产品，不属于 Anet 或
Ahub 核心。它使用非金融化的 Need、Offer、Match、Proposal、Agreement、
Fulfillment 与 Evidence 表达 Agent 协作；钱包、代币、托管和区块链只允许作为
可选适配器。当前 ABA-D0 本地纵向 Demo 不创建 Anet 节点、服务、钱包、支付或
网络连接：

```bash
python experiments/abazr_demo.py
```

Mermaid 架构、路线门禁和信任边界见
[`docs/ABAZR_BLUEPRINT.md`](docs/ABAZR_BLUEPRINT.md)。

WSL 节点现可把 Discord 作为第一条 social discovery adapter。现有 `anet serve`
进程只轮询显式允许的 Guild/Channel；默认仅保留明确提及 Bot 的正文，其余事件
降为元数据。Discord 用户、Guild、Channel 在发往 Anet 前全部变为本地 HMAC
假名；本地账本分别保存证据、标签、信誉分和置信度，并用单调门限决定
`observe/surface/reply/amplify/connect_candidate`。最后一项仍只表示需要人工核验
和显式配对，不能创建 Peer trust 或 capability。配置与边界见
[`docs/DISCORD_SOCIAL_V1.md`](docs/DISCORD_SOCIAL_V1.md)。

```bash
anet --home "$ANET_HOME" discord-social-config \
  --guild '<GUILD_ID>' --channel '<CHANNEL_ID>' \
  --destination '<ALREADY_TRUSTED_NODE_ID>' \
  --content-mode mentions
anet --home "$ANET_HOME" discord-social-status
```

新的控制平面兼容切片保留 PeerCard v1、Node ID 和现有 trust pin，另行定义
不携带地址的 `NodeDescriptor v2`、短期 `ReachabilityRecord v1`，以及把人类
主体与手机 Node ID 分开的设备授权/撤销对象。独立 SQLite checkpoint 使回滚、
分叉和终局撤销跨重启成立；Ahub StoreCarrier 会在所属节点 home 保存签名公开
descriptor 的 `control-state.json` 和短期可达性记录的
`reachability-state.json`，并在同步时持续发布当前候选地址。动态记录不改变
PeerBook trust，也不会把地址自动提升为授权；运行中的节点会把经过 PeerCard 公钥校验的
动态记录作为临时候选，供 direct/health/dialer probe 和 `peer-reachability` CLI 使用，
但不会写入长期 PeerBook；规范与后续边界见
[docs/CONTROL_PLANE_V1.md](docs/CONTROL_PLANE_V1.md)。

P0.2 Ahub 切片把这些公开对象接入私有 allowlist Rendezvous，并以签名请求、
持久防重放 nonce、TTL/配额和领取租约托管现有端到端密文 Packet。锚点无节点
私钥，custody ACK 不等于目的节点 ACK 或业务完成；协议与部署边界见
[`docs/AHUB_V1.md`](docs/AHUB_V1.md)，独立 root、allowlist、TLS 反代、
健康检查、清理和离线备份见
[`docs/AHUB_OPERATIONS.md`](docs/AHUB_OPERATIONS.md)。

Ahub 还提供持久、显式 peer-scoped 的 Relay reservation。两个节点分别用自身
Node ID 签名 WebSocket upgrade，只做主动出站连接；服务端按帧、双向字节、时长、
reservation 和节点连接数限额转发 opaque binary bytes。reservation 跨 Ahub
重启保留，live stream 不保留。显式节点 API 已在该 stream 内运行现有 TLS 1.3、
证书通道绑定、签名身份握手、Packet sync 和 receipt；两个节点可同时关闭常驻
listener/direct。配置 `--live-relay` 后，owner 会为明确 peer 持续刷新 reservation
和出站 listener；对端通过 caller-scoped discovery 自动尝试 TLS Relay，并继续以
Mailbox StoreCarrier 承接离线工作。当前仍是复合 Ahub 路径，尚未完成 P1 的
`SessionCarrier`/`StoreCarrier` 接口拆分。准确边界见
[`docs/RELAY_V1.md`](docs/RELAY_V1.md)。

P0.3 Companion 协议现已固定手机感知与 Agent 干预两个方向的六类对象：
`ObservationBatch`、`Episode`、`Intervention`、`UserResponse`、
`ApprovalRequest` 和 `ApprovalDecision`。节点在发送前和解密落盘前拒绝未知
字段、原始传感器二进制、精确位置/通信正文、心理状态标签，以及未绑定原请求
参数/nonce/scope 的批准决定。Python 与 Android-independent Kotlin 实现读取
同一组语言中立 fixtures，并共同校验规范 JSON SHA-256；Kotlin 核心位于
[`mobile/companion-core`](mobile/companion-core)，完整边界见
[`docs/COMPANION_V1.md`](docs/COMPANION_V1.md)。Node B 侧 Grant/Revocation、
nonce、effect 次数与 fencing 已完成。最小 Android App 已嵌入协议核心，并实现
Room 持久账本、Android Keystore AES-256-GCM payload cipher、本地 consent
安装/撤回清理、加密 Outbox 租约 fencing、Intervention 去重和 UserResponse
原子入队；4 项 Robolectric/Room 测试和 Debug APK 构建已通过。系统通知、
前台服务、Anet 身份/网络、Keystore 真机验证、真实 external executor 和手机
闭环仍未实现。由于当前两台手机已由 Android Remote Control MCP 1.9 承担交互，
Android Companion App 暂停继续开发；已有协议核心和实验 App 保留为未来可复用
资产，不再占用当前 WSL/Discord 主线优先级。

节点可把 Ahub 配成身份签名的自适应 StoreCarrier；HTTP bearer/basic 账号不
参与认证。明文 HTTP 只允许显式 loopback 测试：

```powershell
anet --home <HOME> carrier-add https://ahub.example `
  --type ahub --name public --peer <COMPLETE_PEER_NODE_ID> --live-relay
anet --home <HOME> carrier-list
```

上传只形成该 Ahub 路径的 custody 状态。目标节点本地持久接收后签发
`DestinationSettlement v1`；发送端用已固定目标公钥和本地原始 Packet 验证后
才记录目的 ACK。业务 receipt 与本地 consumer ACK 仍分别独立。

```powershell
anet --home <HOME> dialer-add raw --type raw --priority 0
anet --home <HOME> dialer-add mihomo --type socks5 `
  --url socks5://127.0.0.1:7890 --priority 20
anet --home <HOME> dialer-add radio --type stdio `
  --executable C:\absolute\path\to\adapter.exe `
  --arg fixed-adapter-argument --env ADAPTER_SECRET --priority 30
anet --home <HOME> dialer-list
anet --home <HOME> dialer-set raw --no-enabled
anet --home <HOME> dialer-set raw --enabled
anet --home <HOME> routing-config --direct-race-width 2 --direct-race-delay 0.15
anet --home <HOME> routing-config --carrier-replica-count 2
anet --home <HOME> dialer-probe <PEER_NODE_ID>
anet --home <HOME> dialer-probe <PEER_NODE_ID> --dialer mihomo --require-all
```

旧节点只配置 `direct_proxy` 时，升级后继续只走该代理；第一次执行 `dialer-add` 会把旧有效路径物化进显式列表。每个 `dialer × locator` 记录独立指标，状态中的路径 ID 形如 `direct:mihomo:tls://...`。代理或 stdio adapter 仅改变 TLS 前的字节流建立方式，不进入 Peer Card 或端到端密文。

第二台物理设备的接入任务与权限边界见 [`docs/PHYSICAL_NODE_HANDOFF.md`](docs/PHYSICAL_NODE_HANDOFF.md)；macOS 安装器为 [`scripts/bootstrap-macos.sh`](scripts/bootstrap-macos.sh)。

Anet v0 的目标不是宣称某一种流量“永远无法封锁”。它先解决更基础的问题：

- 平台或单个服务器关闭后，信息和身份仍然存在；
- 中继节点不能读取消息正文和发送者身份；
- 发送者和接收者不必同时在线；
- 相同密文可以跨不同网络和物理媒介传递；
- 任意 Agent runtime 可通过稳定 CLI 读写机器原生对象；
- 底层承载可在后续版本替换，不改变密文对象格式。

## 已实现

- Ed25519 自持签名身份和 X25519 加密身份；
- Peer Card 签名、显式导入和密钥固定；
- 有期限、抗错配的签名配对 Offer/Response；
- 本地持久化节点撤销、peer 作用域密钥退休和运行时即时拒绝；
- 临时 X25519 + HKDF-SHA256 + ChaCha20-Poly1305 端到端封装；
- Ed25519 签名、目标 peer 绑定的一次性 X25519 预密钥包，原子预留、使用后逻辑擦除和 v1/v2 滚动兼容；
- 自动低水位补货；普通消息严格要求 opk 时，零库存仍可通过运行时专用恢复控制对象交换签名公钥；
- TLS 1.3 链路、双向签名挑战及证书通道绑定；
- 无 shell 的外部 `stdio` 字节流适配器，含最小环境、显式变量白名单、启动/退出/超时分类和竞速取消进程回收；
- MessagePack 二进制载荷和 2 的幂次随机填充；
- SQLite WAL 密文队列、去重、TTL、有限跳数和投递回执；
- 多节点受控泛洪，允许中继不知道正文和发送者；
- 离线 Bundle 导出和导入；
- Carrier 插件接口；
- 无监听端口的加密 Directory Carrier，可借任意目录同步、网络盘或物理介质搬运；
- Directory/WebDAV Carrier v2 的双向保管 ACK、抗篡改隔离、每周轮换不透明邮箱和 4 KiB 起始长度桶；
- `control / interactive / normal / bulk` 四级 QoS，字段受端到端对象认证；
- 持久化路径成功率、连续失败/恢复、EWMA RTT 和路由状态；
- 直连故障后的自动 Directory Carrier failover，以及恢复冷却和滞回；
- 标准 WebDAV/HTTPS 哑邮箱 Carrier；节点只发出 HTTPS 请求，凭据只读取环境变量；
- 多级备用路径：选中 Carrier 连续失败后自动转向下一条，首选路径恢复后经滞回切回；
- 1–4 条有界跨 Carrier 副本；默认单副本，紧急 QoS 可在直连首次失败时抢跑到多条异步路径；
- 待发对象即时唤醒、空闲直连低频健康探测、Carrier 指数退避，以及所有后台周期的加密随机抖动；
- 多 Agent durable consumer group、原子租约、ACK/NACK、续租和崩溃后重新投递；
- `benchmark` JSONL 实验记录器和可重复的 Linux namespace/netem 故障实验；
- IPv4、IPv6，以及能提供 IP 套接字的 Yggdrasil/cjdns/Mycelium 等覆盖网地址；
- 文本、结构化对象和二进制文件载荷。

## 手动开发安装

```powershell
cd <SOURCE_ROOT>
python -m pip install -e .
anet --help
```

也可以不安装：

```powershell
$env:PYTHONPATH = "$PWD\src"
python -m anet --help
```

### WSL 发布门禁

`scripts/wsl_release_gate.py` 接收固定 wheel/sdist 哈希、回滚 wheel、节点目录和
systemd user service，自动完成隔离测试、安装、重启、身份与状态对比及失败
回滚。完整证据只写入本地 `0600` JSON；stdout 是不含 Peer Card 和密钥摘要的
单行结果，适合由自动化 Agent 低 token 调用。

```bash
python3 scripts/wsl_release_gate.py --help
```

## 两节点最小演示

创建两个节点：

```powershell
anet --home .\demo\a init --label node_a --host 127.0.0.1 --port 43101
anet --home .\demo\b init --label ahub --host 127.0.0.1 --port 43102

anet --home .\demo\a card --out .\demo\a.card.json
anet --home .\demo\b card --out .\demo\b.card.json

anet --home .\demo\a peer-add .\demo\b.card.json
anet --home .\demo\b peer-add .\demo\a.card.json
```

也可以使用显式挑战—响应配对，避免两张独立 Card 在异步通道中被错配：

```powershell
# A 创建 Offer；文件可通过任意已认证或离线通道交给 B
anet --home .\demo\a pair-offer --out .\demo\a.offer.json --ttl 3600

# B 审核来源后明确接受；此时 B 固定 A，并生成绑定该 Offer 的响应
anet --home .\demo\b pair-accept .\demo\a.offer.json --out .\demo\b.response.json

# A 使用自己保留的原 Offer 验证响应并固定 B
anet --home .\demo\a pair-complete .\demo\a.offer.json .\demo\b.response.json
```

`pair-accept` 不通过 MCP 暴露，避免模型在没有本地审批的情况下扩大信任边界。Offer 默认一小时过期，签名覆盖 Peer Card、随机挑战和有效期；Response 还绑定完整 Offer 摘要，不能移接到另一份 Offer。

若设备丢失、密钥疑似泄漏或不再信任某节点，使用完整 Node ID 做本地撤销：

```powershell
anet --home .\demo\a peer-revoke <PEER_NODE_ID> --confirm <PEER_NODE_ID> --reason "device lost"
anet --home .\demo\a peer-revocations
```

撤销首先写入 fail-closed deny ledger，然后终止该 peer 的待发对象和未完成 Agent claim、把历史 Inbox 降为 untrusted、退休 peer-scoped 预密钥并清除路径状态。该操作无需重启但不可用普通 `peer-add` 或配对撤销；如需重新建立关系，应使用新设备身份。它只在本节点生效，不是全网广播的身份撤销。

分别启动：

```powershell
anet --home .\demo\a serve
anet --home .\demo\b serve
```

取得 B 的节点 ID：

```powershell
anet --home .\demo\b status
```

发送文本或 Agent 对象：

```powershell
anet --home .\demo\a send <B_NODE_ID> --kind message --text "hello"
anet --home .\demo\a send <B_NODE_ID> --kind intent --json-body '{"objective":"benchmark","deadline_ms":30000}'
anet --home .\demo\a send <B_NODE_ID> --kind command --qos control --text "cancel"
anet --home .\demo\b inbox --trusted-only
```

`send` 只负责把密文写入本地耐久队列；常驻 `serve` 或手动 `sync` 负责传递。这样发送时目标不必在线。

### 一次性预密钥

v0.3.7 可让异步单播不再只依赖接收方长期 X25519 私钥。双方固定最新 Peer Card 后，各自生成签名预密钥包并交给对方：

```powershell
anet --home .\demo\a prekey-generate .\demo\a.prekeys.json --peer <B_NODE_ID> --count 100 --ttl-days 30
anet --home .\demo\b prekey-generate .\demo\b.prekeys.json --peer <A_NODE_ID> --count 100 --ttl-days 30

anet --home .\demo\a prekey-import .\demo\b.prekeys.json
anet --home .\demo\b prekey-import .\demo\a.prekeys.json

anet --home .\demo\a prekey-policy require
anet --home .\demo\b prekey-policy require
anet --home .\demo\a prekey-status
```

v2 密钥包的签名同时绑定发布者和唯一目标 peer。即使第三个可信节点取得公开密钥，也不能让接收端消费它；每一对节点维护独立 generation 和库存。`prefer`（默认）在库存存在时使用单次密钥，否则明确降为静态密钥；`require` 在普通消息缺少合格库存时拒绝排队；`disable` 始终不用预密钥。对方只声明 `one-time-prekeys-v1` 时仍可使用旧库存；声明 v2 后不会误用未绑定 peer 的 v1 密钥。

自动补货默认在 v2 可用库存低于 64 时请求 256 个 30 天密钥，失败重试间隔为 15 分钟：

```powershell
anet --home .\demo\a prekey-config `
  --auto --low-watermark 64 --batch-size 256 `
  --request-interval 900 --ttl-days 30

# 运维或测试时立即请求，不等待低水位
anet --home .\demo\a prekey-replenish <B_NODE_ID>
```

请求和响应仍是可经直连、Directory、WebDAV 或 Bundle 搬运的 `SealedPacket`。有 v2 库存时控制对象也使用 opk；双方库存均为零时，运行时仅允许 `network.prekey.request/bundle` 使用 v3 静态信封恢复。该例外只承载请求和目标绑定的签名公钥，Agent 不能通过公开 `send` 伪造这些保留 kind；普通消息在 `require` 下绝不随之降级。控制信封日后被长期静态密钥解开时，可能暴露节点关系和公开预密钥，但不会暴露普通消息正文。

从 v0.3.7 升级时，单-peer 节点自动把旧库存绑定到唯一 peer，并从旧 generation 后继续生成 v2。旧库存曾被多个 peer 共享时归属无法安全推断，节点会停止签发并要求显式处理：

```powershell
# 查看风险；多 peer 时不会自动销毁
anet --home .\demo\a prekey-status

# 明确退休共享私钥，为每个已固定 peer 保留 generation 基线
anet --home .\demo\a prekey-migrate --retire-shared
```

退休可能使仍在途的旧 v1 Packet 无法解密，因此必须显式执行。随后重启节点会自动交换新的 peer-scoped v2 库存。

`send` 与 MCP 结果返回 `packet_version`、`key_mode`、`prekey_id` 和前向保密范围，不会把静态包报告为安全包。对方完全不声明一次性预密钥能力时发送 v2 静态 Packet，保持更早版本的滚动兼容。

接收方只有在密文、签名和发送者作用域均验证，且 Inbox 与投递状态原子提交后，才把对应预密钥私钥设为 `NULL`；提交前崩溃可以重试，提交后相同 Packet 重放无需再次解密。确定性的密文/签名/作用域失败进入本地拒绝账本并停止重启重试，临时磁盘和数据库故障仍保留重试。它保护的是“接收方已擦除预密钥后，攻击者取得长期身份私钥和此前截获的传输密文”这一范围。它不保护仍保存在本机 Inbox 的已解密正文，也不保证 SQLite 备份、虚拟机快照、SSD 历史页或运行中内存已经物理擦除。

## 三节点中继

A 需要保存 B 和 C 的 Card，B 与 A/C 建立直连，C 保存 A/B 的 Card。A 发送给 C 时，外层目的 ID 可见，但正文、消息类型和 A 的身份都在 C 的端到端密文中。B 保存密文并在 C 恢复连接后继续转发。

当前路由是有跳数和去重保护的受控泛洪，适合小规模私有网络；它不是大规模匿名路由协议。

## 离线传递

```powershell
anet --home .\demo\a bundle-export .\carry.anet --destination <B_NODE_ID>
anet --home .\demo\b bundle-import .\carry.anet
```

Bundle 中仍然只有端到端密文，可以通过 U 盘、局域网文件共享或其他媒介移动。导入节点会验证每个包的结构、有效期和大小，并自动去重。

## 无监听 Directory Carrier

Directory Carrier 不要求节点开放 TCP/UDP 端口。双方只需能以某种方式复制同一个投递目录；同步工具、WebDAV 挂载、网络盘、U 盘和人工携带都只是“不可信搬运工”。Anet 会为每一对节点、每一个方向和每个时间窗口派生不同的不透明邮箱名，并再次加密、签名投递帧。

v2 邮箱每 7 天轮换，接收方覆盖当前、时钟偏差窗口和 31 天离线保留期；旧版静态邮箱仍可读取。发送方只在对方签名 Peer Card 声明 v2 能力后才启用新格式，因此滚动升级不会让旧节点失联。已有节点升级后应重新导出并相互 `peer-add` 最新 Card，才能从兼容 v1 切换到 v2。

只交换公钥、不公布直连地址：

```powershell
anet --home .\demo\a card --keys-only --out .\demo\a.keys.json
anet --home .\demo\b card --keys-only --out .\demo\b.keys.json
```

执行一次异步同步：

```powershell
anet --home .\demo\a carrier-sync D:\anet-drop
anet --home .\demo\b carrier-sync D:\anet-drop
```

或者只运行目录轮询器，不启动任何入站网络监听：

```powershell
anet --home .\demo\a carrier-serve D:\anet-drop --interval 2
```

把载体加入常驻守护进程并配置弹性路由：

```powershell
anet --home .\demo\a carrier-add D:\anet-drop `
  --name fallback-a --peer <B_NODE_ID> --mode fallback --interval 1

anet --home .\demo\a routing-config `
  --failure-threshold 2 --recovery-threshold 3 `
  --carrier-failure-threshold 2 --carrier-recovery-threshold 3 `
  --direct-retry-interval 5 --direct-idle-probe-interval 60 `
  --direct-probe-jitter 0.35 --fallback-probe-interval 5 `
  --fallback-probe-jitter 0.35 --sync-jitter 0.2 --cooldown 30
```

`fallback` 模式下，第一次直连失败会让 `control` 和 `interactive` 消息抢先走最佳备用路径；达到失败阈值后，`normal` 和 `bulk` 也整体切换。备用期间继续探测直连，只有连续恢复达到阈值并经过冷却期才切回。

后台不会再在空闲时每个 `sync_interval` 都建立直连。新 Packet 出现会在下一次本地调度 tick 立即唤醒对应路径；失败重试使用较短的有抖动间隔，空闲健康探测则从 60 秒开始逐步退避。Directory/WebDAV 轮询默认加入 ±25% 抖动，连续无活动时最多退避到基础间隔的 4 倍；显式 `sync`、`probe` 和 `benchmark` 仍可强制立即执行。可在 `carrier-add` 使用 `--jitter` 与 `--idle-backoff-max` 调整。

也可以运行完全无监听、只依靠已配置 Carrier 的节点：

```powershell
anet --home .\demo\a routing-config --no-listen --no-direct
anet --home .\demo\a serve
```

### SOCKS5 直连代理

已有本机代理链可以承载 Anet 直连 TCP，再由 Anet 在隧道内执行原有 TLS 1.3 和签名 peer 握手：

```powershell
anet --home .\demo\a direct-proxy socks5h://127.0.0.1:1080
anet --home .\demo\a direct-proxy
anet --home .\demo\a direct-proxy --clear
```

`socks5://` 在本机解析 peer 域名并向代理发送 IP；`socks5h://` 让代理解析域名。RFC1929 认证只保存环境变量名，且必须同时提供两者：

```powershell
$env:ANET_SOCKS_USER = "runtime secret"
$env:ANET_SOCKS_PASS = "runtime secret"
anet --home .\demo\a direct-proxy socks5h://127.0.0.1:1080 `
  --username-env ANET_SOCKS_USER --password-env ANET_SOCKS_PASS
```

命令和 `status` 只输出 URL、许可标志和环境变量名，不读取或打印值。默认仅允许回环代理；远端代理必须显式使用 `--allow-remote`，该选择会写入配置。设置和清除都返回 `restart_required: true`。URL 内嵌凭据、路径、查询、fragment 和无效端口会被拒绝。

### WebDAV/HTTPS Carrier

WebDAV 服务器只需要提供 `MKCOL / PROPFIND / PUT / GET / DELETE / MOVE`，其中基础 collection 的 `PROPFIND Depth: 1` 用来枚举已存在邮箱。Anet 在 HTTPS 内再次使用与 Directory Carrier 相同的配对信道加密、签名和不透明对象名。客户端不会逐个请求尚不存在的未来/历史秘密邮箱，避免由服务端请求日志反向关联轮换 token。不要把用户名、密码或 token 写入 URL：

```powershell
$env:ANET_DAV_TOKEN = "由服务商签发的 token"

anet --home .\demo\a carrier-add https://dav.example/path `
  --type webdav --name dav-fallback --peer <B_NODE_ID> `
  --mode fallback --bearer-env ANET_DAV_TOKEN --priority 200
```

Basic Auth 使用 `--username-env` 与 `--password-env`。明文 HTTP 只允许在显式指定 `--allow-insecure-http` 时连接本机回环地址，不能用于远端节点。Anet 拒绝 URL 内嵌凭据和 HTTP 重定向，避免把认证信息送往另一个主机。

WebDAV 是路径多样性组件，不是“HTTPS 就不可封锁”的承诺。服务商仍能看到账号、访问时间、长度桶和已存在的不透明邮箱，也能删除、延迟或回滚对象。v2 将 ACK 与常见小消息共同填充到至少 4 KiB，并对更大帧使用 2 的幂次桶；它降低消息类型和精确长度泄漏，但没有消除时间关联。

同一条端到端密文可在 TLS 直连、Directory Carrier 和离线 Bundle 之间搬运。Directory Carrier 隐藏投递帧里的节点 ID、包 ID、类型、ACK 与正文，但目录服务商仍可观察文件时间、长度桶和邮箱活动，也可以删除或延迟文件。

## 与 Agent 集成

结构化输入：

```powershell
'{"performative":"PROPOSE","confidence":0.81}' |
  anet --home $env:ANET_HOME send <NODE_ID> --kind agent.ir --stdin --stdin-format json
```

结构化输出：

```powershell
anet --home $env:ANET_HOME inbox --unread --trusted-only --mark-read
```

CLI 始终输出 JSON，适合由 Agent runtime、自动化 runner、PowerShell
或其他进程调用。

### 多 Agent 可靠消费

普通 `inbox --mark-read` 只适合人工查看。多个 Agent 处理任务时应使用消费组；同一组中的 worker 竞争任务，不同组则各自获得一份消息：

```powershell
# 默认 latest：只消费建组后到达的新对象，避免误执行历史消息
anet --home $env:ANET_HOME consumer-open runtime-a.tasks `
  --kind-prefix agent.runtime-a. --trusted-only

anet --home $env:ANET_HOME consumer-claim runtime-a.tasks `
  --owner runtime-a --limit 1 --lease-seconds 300

# 只有在结果和副作用均已可靠完成后 ACK
anet --home $env:ANET_HOME consumer-settle runtime-a.tasks <CLAIM_TOKEN> `
  --owner runtime-a --action ack

# 临时失败则 NACK；对象在延迟后重新变为可领取
anet --home $env:ANET_HOME consumer-settle runtime-a.tasks <CLAIM_TOKEN> `
  --owner runtime-a --action nack --retry-seconds 60 --error "temporary failure"
```

长任务应在租约到期前运行 `consumer-renew`。进程崩溃后租约自动过期，其他 worker 会得到新 token 和递增的 `delivery_attempt`；旧 token 不能 ACK 新租约。消费组过滤条件创建后不可静默改变，`latest` 使用 SQLite Inbox rowid 边界，不依赖毫秒时钟。

发送者签名只证明来源，不代表其载荷指令安全。每个 claim 都携带 `content_security` 警告；Agent 必须在本地重新执行权限、预算、参数和副作用审批。

主动测量实际确认路径：

```powershell
anet --home $env:ANET_HOME probe <NODE_ID> --qos control --timeout 20
```

结果包含端到端耗时、路由切换前后状态，以及该 Packet 实际在哪个 `path_id` 上取得 custody ACK；网络探针与内部 receipt 不会进入普通 Agent Inbox。

连续实验并写入逐探针 JSONL：

```powershell
anet --home $env:ANET_HOME benchmark <NODE_ID> `
  --count 20 --spacing 1 --timeout 20 `
  --out .\runtime\experiments\wan.jsonl
```

带宽实验可加入机器随机载荷，例如 `--qos bulk --payload-bytes 262144`。由于 Anet 会填充到下一个 2 的幂次桶，262,144 字节应用载荷在当前编码下约形成 512 KiB 密文桶，不能直接用应用载荷大小冒充线上字节数。

Linux/WSL 的可重复 netem 实验见 [experiments/README.md](experiments/README.md)。它使用独立 network namespace 与 veth，不修改生产接口。

### 持续本地观测

低频长期探针会将每一条结果立即写入 JSONL，并给发送间隔加入随机抖动：

```powershell
anet --home $env:ANET_HOME monitor <NODE_ID> `
  --out "$env:ANET_HOME\monitor.jsonl" `
  --interval 60 --jitter 0.35 --timeout 20
```

Windows 后台脚本：

```powershell
.\scripts\start-monitor.ps1 -Destination <NODE_ID> -Interval 60 -Jitter 0.35
.\scripts\status-monitor.ps1
.\scripts\stop-monitor.ps1
```

监测默认不自动启用。即使正文加密，固定或高频探针也会暴露活动规律；JSONL 包含 Node ID、时间、延迟和路径，必须按敏感元数据保护。transient probe/receipt 默认保留 7 天后清理。

### MCP 适配器

Anet 还提供独立的 stdio MCP Server：

```powershell
$env:ANET_HOME = "C:\Anet\nodes\runtime-a"
anet mcp
```

它暴露状态、Peer Card、发送、同步、探针、durable consumer，以及 typed task 工具 `anet_task / anet_task_begin / anet_task_settle`。后两者用持久 task ledger 保证逻辑任务领取幂等，并把任务结果与 consumer ACK 放在同一 SQLite 事务；详细流程见 [`docs/AGENT_TASK_PROTOCOL.md`](docs/AGENT_TASK_PROTOCOL.md)。通用配置示例见 [mcp-stdio.example.json](mcp-stdio.example.json)，完整 Agent 接入说明见 [`docs/MCP_AGENT_GUIDE.md`](docs/MCP_AGENT_GUIDE.md)。MCP 进程默认不占用网络监听端口；入站连接仍由单独的 `anet serve` 守护进程负责，避免多个 Agent runtime 各自争用同一端口。

生产 profile 应设置进程级 capability：`ANET_AGENT_ID` 固定 claim owner，`ANET_MCP_GROUP_PREFIX` 和 `ANET_MCP_KIND_PREFIX` 隔离本地队列，`ANET_MCP_ALLOWED_PEERS` 限制出站目标，`ANET_MCP_TASK_ALLOWED_SENDERS` 限制入站任务发送者，`ANET_MCP_TASK_CAPABILITIES` 只授权精确 capability 或显式 `namespace.*`，并用 `ANET_MCP_ALLOW_RAW_INBOX=0` 禁止绕过租约读取全量 Inbox。私有关系活动默认保持 `ANET_MCP_ALLOW_RELATION_ACTIVITY=0`，只有确实需要观察本地社交模型的 Agent 才显式开启。受众绑定的关系披露也是独立的默认关闭能力（`ANET_MCP_ALLOW_RELATION_DISCLOSURE=0`）；它只发送无正文视图，接收结果不会写入本机圈层、信任或授权，详见 [`docs/RELATIONSHIP_DISCLOSURES_V1.md`](docs/RELATIONSHIP_DISCLOSURES_V1.md)。需要展示远端观察者的圈层时，使用 `relation-reported-view <SENDER_NODE_ID>` 生成带来源的报告视图；持续披露使用 v2 series 来证明 cursor 连续或暴露缺页，但连续不等于完整现实或当前状态。受众可以发送只标明可见缺失序号的咨询性 gap notice，但这不是拉取请求；观察者只有在原计划仍有效时才能补发原始归档页，不能扩大范围或推进 series。入站 sender 默认全部拒绝，capability 默认只允许空需求任务；工具参数不能扩大这些环境能力。

Agent profile 内其他 MCP 必须同样健康。某些客户端会在构建 Agent 时初始化
profile 中所有 enabled MCP；一个无关 server 的启动失败可能关闭已发现的 Anet
session。无人值守 worker 应使用只启用必要 MCP 的最小 profile，并把客户端
生命周期故障与 Anet 网络故障分开诊断。

## 重要边界

- TLS/TCP 端点仍可能按 IP、端口或流量行为被发现和阻断；Directory Carrier 的可达性则取决于承载该目录的外部复制路径。Anet 不声称已经解决所有 DPI 或主动封锁。
- 外层目的节点、密文长度桶、时间和相邻节点关系仍可能泄漏；邮箱轮换不能对同一账号的全局服务商隐藏所有关系。
- 随机抖动和空闲退避只移除严格固定周期，不是 cover traffic 或匿名性证明；真实消息仍会导致可观察的活动突发。
- v0.3 没有 onion routing、mixnet、cover traffic、自动 NAT 穿透、MLS 群组和硬件密钥保护。
- QoS 和最终目的 ID 位于加密正文之外，虽受认证但会向直接中继暴露；Directory Carrier 的第二层封装会隐藏这些字段。
- v3 单播在双方交换库存后可使用一次性预密钥，降低长期静态 X25519 密钥日后泄漏对已截获 Packet 的影响；静态降级包、Directory/WebDAV 配对帧、本地明文 Inbox 和尚未消费的预密钥不在该保证内，也尚无 Double Ratchet 的失陷后恢复。
- 私钥目前存放在本地文件；生产使用应迁移到 TPM、硬件密钥或受保护的系统密钥库。
- 未导入 Card 的发送者会进入 untrusted inbox，不会获得自动投递回执。

完整协议、威胁边界、验证证据和版本变化见 [PROTOCOL.md](PROTOCOL.md)、[SECURITY.md](SECURITY.md)、[VERIFICATION.md](VERIFICATION.md) 与 [CHANGELOG.md](CHANGELOG.md)。

## Windows 节点管理模板

每个持久 runtime 必须使用独立私有 node home、独立身份和独立监听端口。默认只监听回环地址，不会自动向局域网或公网开放。管理脚本：

```powershell
.\scripts\start-node.ps1 -NodeHome <HOME>
.\scripts\status-node.ps1 -NodeHome <HOME>
.\scripts\stop-node.ps1 -NodeHome <HOME>
```

不要手改 `config.json` 或签名 Card。使用不透明随机 zone 配置同机和 LAN 范围；以下字符串只是模板：

```powershell
anet --home <HOME> locator-config `
  --add-context host:<HOST_ZONE> `
  --add-context lan:<LAN_ZONE> `
  --advertise "tls://<SHARED_HOST_ADDRESS>:<PORT>?scope=host&zone=<HOST_ZONE>&priority=0" `
  --advertise "tls://<LAN_IP>:<PORT>?scope=lan&zone=<LAN_ZONE>&priority=20"
anet --home <HOME> doctor
```

Windows 与同机 WSL 使用相同 `host:<HOST_ZONE>` 和两端都能到达的
`<SHARED_HOST_ADDRESS>`，但仍使用不同端口和 Node ID；不能把 `127.0.0.1`
作为跨运行时 host-scoped 地址。Mac 只共享 `lan:<LAN_ZONE>`，绝不能共享
Windows/WSL 的 host zone。`locator-config` 原子更新配置并重签 `card.json`；随后必须重新交换 Card。不要把 `identity.json`、`tls-key.pem`、数据库或完整 node home 发送给任何其他节点。
