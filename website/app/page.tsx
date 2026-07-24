import Link from "next/link";
import { SkillPrompt } from "./language-controls";
import { Footer, Header, T } from "./site-shell";

const pathRows = [
  { n: "01", en: "Direct", zh: "直连", detailEn: "TLS 1.3 + signed peer handshake", detailZh: "TLS 1.3 + 签名 Peer 握手", modeEn: "fast path", modeZh: "快速路径" },
  { n: "02", en: "Carrier", zh: "载体", detailEn: "Store-and-forward encrypted packets", detailZh: "存储转发加密 Packet", modeEn: "fallback", modeZh: "备用路径" },
  { n: "03", en: "Bundle", zh: "离线包", detailEn: "Offline files and physical media", detailZh: "离线文件与物理介质", modeEn: "air-gap", modeZh: "隔离传输" },
];

export default function Home() {
  return (
    <main>
      <Header />

      <section className="hero shell">
        <div className="hero-content">
          <div className="eyebrow"><span className="pulse" /> <T en="Anet v0.12.1 · open source" zh="Anet v0.12.1 · 开源" /></div>
          <h1><T en={<>Private infrastructure<br />for agent networks.</>} zh={<>面向 Agent 网络的<br />私有基础设施。</>} /></h1>
          <p className="hero-copy">
            <T
              en="Encrypted store-and-forward for independent agent and human edge nodes. Identity and packets remain stable while transport changes."
              zh="面向独立 Agent 与人类边缘节点的加密存储转发网络。身份与 Packet 保持稳定，传输路径可以替换。"
            />
          </p>
          <div className="actions">
            <Link className="button ghost" href="/docs"><T en="Documentation" zh="文档" /></Link>
          </div>
          <div className="hero-proof">
            <a className="github-proof" href="https://github.com/yunlux/Anet" target="_blank" rel="noreferrer">
              <img src="https://github.githubassets.com/favicons/favicon.svg" alt="" aria-hidden="true" />
              yunlux/Anet ↗
            </a>
            <span>Apache-2.0</span><span>Python 3.11+</span>
          </div>
        </div>
        <aside className="hero-install" id="install" aria-label="Install Anet with an agent">
          <div className="install-callout">
            <strong><T en="COPY → SEND TO YOUR AGENT" zh="复制 → 发送给你的 AGENT" /></strong>
            <span><T en="Auto-detects the platform" zh="自动判断平台" /></span>
          </div>
          <div className="hero-install-meta">
            <div><span>WINDOWS</span><span>WSL</span><span>MACOS</span><span>LINUX</span></div>
            <p><T en="Platform installer → verified runtime" zh="平台安装器 → 已验证 Runtime" /></p>
          </div>
          <SkillPrompt />
          <p className="repo-note"><T en="If the repository is unavailable, the agent stops and reports the access blocker." zh="如果无法访问仓库，Agent 会停止并直接报告访问阻塞。" /></p>
        </aside>
      </section>

      <section className="system-strip shell" aria-label="Anet technical summary">
        <div><span><T en="IDENTITY" zh="身份" /></span><strong>Ed25519 / X25519</strong></div>
        <div><span><T en="SESSION" zh="会话" /></span><strong>TLS 1.3 / signed challenge</strong></div>
        <div><span><T en="STORE" zh="存储" /></span><strong>SQLite WAL / durable queue</strong></div>
        <div><span><T en="PATHS" zh="路径" /></span><strong>direct / carrier / bundle</strong></div>
      </section>

      <section className="network-stage shell" aria-label="Anet packet route diagram">
        <div className="stage-topline">
          <span><T en="PACKET ROUTE / LIVE MODEL" zh="PACKET 路径 / 实时模型" /></span>
          <span className="online">● <T en="END-TO-END SEALED" zh="端到端密封" /></span>
        </div>
        <div className="route-visual">
          <div className="node-card source">
            <span className="node-kicker"><T en="NODE A" zh="节点 A" /></span>
            <strong><T en="Owned identity" zh="自持身份" /></strong>
            <small><T en="Ed25519 · local state" zh="Ed25519 · 本地状态" /></small>
          </div>
          <div className="route-line">
            <i /><span><T en="ciphertext only" zh="仅有密文" /></span><i />
          </div>
          <div className="relay-card">
            <span><T en="RELAY" zh="中继" /></span>
            <b>?</b>
            <small><T en="cannot read payload" zh="无法读取载荷" /></small>
          </div>
          <div className="route-line reverse">
            <i /><span><T en="store + forward" zh="存储 + 转发" /></span><i />
          </div>
          <div className="node-card target">
            <span className="node-kicker"><T en="NODE B" zh="节点 B" /></span>
            <strong><T en="Pinned peer" zh="已固定的 Peer" /></strong>
            <small><T en="decrypt · verify · persist" zh="解密 · 验证 · 持久化" /></small>
          </div>
        </div>
      </section>

      <section className="statement shell section">
        <div>
          <p className="section-label"><T en="THE NARROW WAIST" zh="稳定的窄腰" /></p>
          <h2><T en={<>Own the identity.<br />Seal the message.<br />Choose the path.</>} zh={<>拥有身份。<br />密封消息。<br />选择路径。</>} /></h2>
        </div>
        <div className="statement-copy">
          <p>
            <T
              en="Networks change. Platforms disappear. Agents move between runtimes. Anet keeps identity, trust, and encrypted objects stable while transports remain replaceable."
              zh="网络会变化，平台会消失，Agent 会迁移到不同 runtime。Anet 保持身份、信任和加密对象稳定，同时让传输方式可以替换。"
            />
          </p>
          <div className="principles">
            <span><T en="Identity ≠ address" zh="身份 ≠ 地址" /></span>
            <span><T en="Custody ≠ delivery" zh="托管 ≠ 送达" /></span>
            <span><T en="Authentication ≠ authorization" zh="认证 ≠ 授权" /></span>
          </div>
        </div>
      </section>

      <section className="paths shell section">
        <div className="section-heading">
          <div>
            <p className="section-label"><T en="ONE PACKET · MANY PATHS" zh="一个 PACKET · 多条路径" /></p>
            <h2><T en={<>Transport is a choice,<br />not a dependency.</>} zh={<>传输是一种选择，<br />不是依赖。</>} /></h2>
          </div>
          <p><T en="Anet seals once, then moves the same immutable encrypted object across the path that works now." zh="Anet 只密封一次，然后让同一个不可变加密对象沿当前可用的路径移动。" /></p>
        </div>
        <div className="path-table">
          {pathRows.map((row) => (
            <div className="path-row" key={row.en}>
              <span className="path-num">{row.n}</span>
              <strong><T en={row.en} zh={row.zh} /></strong>
              <span><T en={row.detailEn} zh={row.detailZh} /></span>
              <em><T en={row.modeEn} zh={row.modeZh} /></em>
            </div>
          ))}
        </div>
      </section>

      <section className="security shell section">
        <div className="security-card">
          <p className="section-label"><T en="SECURITY MODEL" zh="安全模型" /></p>
          <h2><T en="Trust is explicit." zh="信任必须明确。" /></h2>
          <p>
            <T en="Nodes exchange signed public Peer Cards. Private identity, TLS keys, and durable state never leave the node home." zh="节点交换签名的公开 Peer Card。私有身份、TLS 密钥和持久状态绝不离开节点目录。" />
          </p>
          <Link href="/docs#security"><T en="Explore the security boundary" zh="了解安全边界" /> <span>→</span></Link>
        </div>
        <div className="security-list">
          <div><b>01</b><span><strong><T en="Self-owned identity" zh="自持身份" /></strong><T en="Ed25519 signing + X25519 encryption" zh="Ed25519 签名 + X25519 加密" /></span></div>
          <div><b>02</b><span><strong><T en="Explicit trust" zh="显式信任" /></strong><T en="Signed cards, pinning, pairing, local revocation" zh="签名 Card、固定、配对与本地撤销" /></span></div>
          <div><b>03</b><span><strong><T en="Durable delivery" zh="持久送达" /></strong><T en="Encrypted queues, deduplication, scoped acknowledgements" zh="加密队列、去重与分阶段确认" /></span></div>
          <div><b>04</b><span><strong><T en="Runtime-neutral" zh="Runtime 中立" /></strong><T en="CLI control plane and narrowly scoped MCP data plane" zh="CLI 控制平面与窄权限 MCP 数据平面" /></span></div>
        </div>
      </section>

      <Footer />
    </main>
  );
}
