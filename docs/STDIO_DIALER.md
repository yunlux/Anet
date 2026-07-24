# Anet stdio 字节流适配器

## 目的

`stdio` dialer 把 Anet 的直连 TLS 与某一种固定 socket API 解耦。外部 adapter 只需提供可靠、有序、双向的字节流，就可以把 Anet 搬到串口、radio modem、SSH `-W`、自定义 relay、覆盖网络 CLI 或实验性物理层之上。

它不是新的信任根，也不是新的消息协议。链路建立后仍由 Anet 完成 TLS 1.3、证书指纹通道绑定、双向签名 Node ID 握手、Packet 同步、去重和 ACK。adapter 看见 TLS ciphertext、目标 locator、时间和长度，但不取得 Anet 私钥或消息明文。

## 进程契约

Anet 使用 `asyncio.create_subprocess_exec(executable, *args)` 直接启动 adapter：

- 不启动 PowerShell、`cmd.exe`、`sh` 或其他 shell；
- `executable` 必须是绝对路径；
- argv 是预先配置的固定字符串数组，最多 32 项，每项 1–4096 字符，拒绝 NUL 和换行；
- adapter 从 stdin 读取 Anet 发出的 TLS bytes；
- adapter 把远端返回的 TLS bytes 原样写到 stdout；
- stdout 不能混入日志、状态行或 framing；日志应写 stderr，当前 Anet 丢弃 stderr，避免秘密进入日志；
- EOF 表示关闭当前字节流；每次 dial/probe 启动一个新进程。

目标由 Anet 设置两个保留环境变量：

```text
ANET_TARGET_HOST
ANET_TARGET_PORT
```

adapter 不应把它们解释为已认证身份。远端身份最终由 TLS 内的签名 Peer Card 握手确认。

## 环境与秘密

子进程不会继承整个 Agent 环境。Anet 只复制 Windows/POSIX 启动所需的少量平台变量，以及管理员通过重复 `--env NAME` 显式列出的变量。配置保存和 CLI 输出只包含变量名，不包含值。列入白名单等于授权 adapter 读取该值；不要把 Anet 身份私钥、数据库密钥或无关 provider token 加入白名单。

## 配置

PowerShell 示例：

```powershell
anet --home <HOME> dialer-add serial-radio --type stdio `
  --executable C:\Adapters\anet-radio-bridge.exe `
  --arg COM7 `
  --arg 115200 `
  --env RADIO_PSK `
  --startup-timeout 10 `
  --priority 30

anet --home <HOME> dialer-list
anet --home <HOME> dialer-probe <PEER_NODE_ID> --dialer serial-radio
```

Linux/macOS 示例：

```bash
anet --home "$HOME/.anet" dialer-add custom-link --type stdio \
  --executable /opt/anet/bin/custom-link \
  --arg fixed-profile \
  --env LINK_SECRET \
  --priority 20
```

任何增删或启停 dialer 后都要重启常驻节点。先运行 `dialer-probe`，确认返回 `authenticated`，再用非敏感 Probe 做端到端 receipt 验证。不要仅以“adapter 进程启动成功”判断链路可用。

若需要 SSH，建议实现一个固定、经过审计的薄 adapter：读取 `ANET_TARGET_HOST/PORT`，再用 argv 调用 `/usr/bin/ssh ... -W host:port`。不要把目标或模型输出拼成 shell 命令。

## 生命周期和故障

一次连接成功、失败、超时或被 hedged race 取消后，Anet 都会关闭 TLS、socketpair 和 pipe，等待 adapter 短暂自然退出，然后只终止该次启动的精确子进程；不会按名称杀进程。取消的落后候选不计为网络失败。

连接阶段故障会分类为：

- `adapter_config`：白名单变量缺失等本地配置错误；
- `adapter_spawn`：可执行文件不存在、无权限或无法启动；
- `adapter_exit`：TLS 建立前 adapter 已退出；
- `adapter_timeout`：启动或 TLS 字节流建立超时；
- `adapter_protocol`：进程仍在，但没有提供有效 TLS 双向字节流。

TLS 建立后的失败继续使用 `identity_handshake`、`health_protocol` 或 `sync_protocol` 分类。

## 编写 adapter 的最小规则

1. 将 stdin/stdout 当成原始 bytes，不做文本编码。
2. 保持顺序、不得静默截断，不要自行重放旧 session。
3. 支持半关闭或在任一方向 EOF 后尽快关闭另一端。
4. 对缓冲写入及时 flush，避免握手死锁。
5. 将目标解析、radio framing、纠错和重连限制在 adapter 内；不要解析 Anet TLS plaintext。
6. 收到终止信号或管道断开时清理串口、socket 和子进程。
7. 固定依赖、校验发布哈希，并限制 adapter 文件的本地写权限。

低带宽、高延迟、间歇链路如果不能稳定提供一条会话期双向流，更适合实现异步 Carrier 或 Bundle adapter，而不是假装成直连。stdio dialer 本身不提供流量混淆、匿名性、抗主动探测或抗干扰保证；这些性质必须由具体 adapter 和真实受限网络实验分别证明。
