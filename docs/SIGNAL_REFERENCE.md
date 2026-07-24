# Signal 对 Anet 的架构启发

本文只把 Signal 当作经过大规模部署验证的人类异步加密通信参考，不把
Signal 账号体系、中心服务或 wire format 作为 Anet 的兼容目标，也不自行
改写其密码协议。

## Signal 实际解决的是一组分层问题

Signal 的安全并非来自单一“双棘轮”：

1. PQXDH 使用身份键、签名预密钥和一次性预密钥，让发送方可在接收方离线时
   建立初始共享秘密；
2. Double Ratchet 在会话内为每条消息派生并删除独立消息键，提供前向保密，
   并在新的 DH 熵进入后获得妥协后恢复能力；
3. Sesame 管理异步、多设备环境中的活动/非活动会话、设备增删、重试、回执、
   乱序、过期和有界清理；
4. Sealed Sender 让投递服务主要知道目的地，而把发送者身份放在接收端可验证
   的加密内层；短期凭证和 delivery token 分别承担认证与滥用控制；
5. safety number 与 key transparency 使目录不能悄悄为同一账号返回不同身份键；
6. 私有群组和 username/contact discovery 继续减少服务端可见的社交图信息。

截至 2026-07，Signal 已公开 PQXDH、Double Ratchet、SPQR/Triple Ratchet 与
Sesame 规范。Anet 不应根据概述自行创造“类似 Signal”的密码协议；任何引入
必须采用经过分析的标准构造、固定版本和互操作测试。

官方资料：

- [PQXDH](https://signal.org/docs/specifications/pqxdh/)
- [Double Ratchet、SPQR 与 Triple Ratchet](https://signal.org/docs/specifications/doubleratchet/)
- [Sesame 多设备会话管理](https://signal.org/docs/specifications/sesame/)
- [Sealed Sender](https://signal.org/blog/sealed-sender/)
- [Automatic Key Verification](https://support.signal.org/hc/en-us/articles/10223569377562-Automatic-Key-Verification)
- [私有群组系统](https://signal.org/blog/signal-private-group-system/)
- [username 与号码隐私](https://signal.org/blog/phone-number-privacy-usernames/)

## 与当前 Anet 的对应关系

| Signal 层 | Anet 已有能力 | 当前缺口 |
| --- | --- | --- |
| 离线建链 | peer-scoped 签名一次性预密钥；离线 store-and-forward | 尚无标准化的混合 PQ 初始会话 |
| 会话演进 | 每包临时 X25519；OPK 使用后退休 | 没有每设备、每 peer 的 root/send/receive ratchet；没有妥协后恢复 |
| 多设备 | 每个 runtime 拥有独立 Node ID 和私有 home | 没有“根 Agent/主体 → 多设备 Node ID”的可验证层级与设备清单 |
| 乱序与重放 | Packet ID 去重、TTL、ACK、at-least-once 收敛 | 没有 ratchet message number、`MAX_SKIP` 和有界 skipped-key cache |
| 身份验证 | 签名 PeerCard、显式 pin、pairing、local revocation | 没有 card 序列号、身份变更日志或 key transparency |
| 元数据保护 | sender 位于加密 payload；轮换不透明 mailbox；多 Carrier | relay 滥用控制仍缺少不暴露长期 sender 的短期投递凭证 |
| 群组 | 尚未实现 | membership epoch、成员移除后的重键与群组元数据隐私 |

Packet 去重与 ratchet 防重放不能合并成同一概念：前者使跨 Carrier 重复投递不
产生第二条业务消息；后者约束会话密钥状态、乱序窗口和旧消息键的生命周期。

## 建议采用的设计原则

### 1. 根主体与设备节点分离

一个逻辑 Agent、组织或人类主体可以授权多个设备节点，但每个 Windows、WSL、
macOS、移动设备或 worker 仍拥有独立 Node ID、私钥和 `ANET_HOME`。根主体只
签发有序、可撤销、带有效期的设备授权；绝不在设备间复制 `identity.json`、
TLS key、SQLite 或 ratchet state。

### 2. 初始建链与持续会话分离

保留现有 OPK 作为兼容传输模式。新的实验协议应明确拆为：

- 异步 bootstrap：取得经过验证的设备身份和 prekey bundle；
- session：每一对设备维护独立发送链与接收链；
- packet：继续作为 Carrier 无关、可复制的不可变密文对象；
- task：继续由 typed task ledger 负责授权、幂等和副作用。

密码会话不能替代 Agent capability；能解密只证明持有会话状态，不代表有权执行
工具或产生外部副作用。

### 3. 所有失控增长都必须有界

参考 Sesame 和 Double Ratchet，为设备数、非活动会话、未确认明文重试记录、
skipped keys、单次追赶、重试循环和保留时长设置协议上限。状态更新应与密文
持久化或消费在同一事务内，解析/认证失败时回滚全部会话状态。

### 4. 身份变化不能静默

PeerCard 应增加单调序列、前序摘要和显式 rotation/revocation 语义。目录或
gateway 只能提供发现证据，不能自行成为信任根。后续可用 append-only
transparency log 和跨观察者 consistency proof 检测同一主体的 split view。

### 5. relay 的认证与长期身份解耦

可研究接收端签发的短期 delivery capability：relay 可据此限流、拒绝滥用并
定位 mailbox，但不能由 token 直接恢复长期 sender Node ID。内层仍由接收节点
验证完整 sender、PeerCard 和 capability。该机制不能削弱未知发送者默认拒绝。

## 不应照搬的部分

- 不引入电话号码注册、中心账号恢复、push 服务或全局可搜索用户名作为 Anet
  根身份；
- 不把一个主体的私钥或会话状态复制到多个 runtime；
- 不把 typing、presence、read receipt 等人类聊天功能放进基础窄腰；
- 不把中心服务返回的设备目录当作无需验证的事实；
- 不复制 Signal 的私有群组实现来替代 MLS 评估；
- 不直接并入 `signalapp/libsignal` 或 `Signal-Server` 源码。两者当前使用
  AGPL-3.0，而 Anet 是 Apache-2.0；协议思想和公开规范可研究，代码复用需单独
  完成法律与架构评估。

## 推荐实施顺序

1. **Device authorization v1**：根主体、设备 Node ID、序列号、过期、撤销和
   PeerCard 轮换证据，只做身份层，不改 Packet 加密；
2. **Session state model**：定义每设备 peer session、事务边界、乱序上限、
   secure-delete 语义和崩溃恢复测试，先不启用新密码；
3. **标准 ratchet 实验**：选定有审计实现和明确许可的构造，在独立 capability
   下与 v3 OPK 并行，做丢包、乱序、重放、回滚和设备失陷恢复测试；
4. **Transparency log**：先实现本地 card history 与 gossip consistency，再
   评估可选公共/联盟日志；
5. **Sealed delivery capability**：在真实 relay 滥用模型和元数据测量后实验；
6. **群组**：优先评估 MLS，独立处理 membership privacy，不把一对一 ratchet
   直接扩展成自创群组协议；
7. **PQ**：把 PQXDH/Triple Ratchet 作为版本化迁移目标；在依赖、消息尺寸、
   状态和回滚策略成熟前，不宣称量子安全。

每一步都需要 transcript vectors、状态机属性测试、crash/restart、并发、乱序、
丢包、重复、恶意目录和旧版本降级测试；营销名称不能代替这些证据。
