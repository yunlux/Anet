import type { Metadata } from "next";
import Link from "next/link";
import { Footer, Header, T } from "../site-shell";

export const metadata: Metadata = {
  title: "Documentation",
  description: "Understand Anet, create isolated nodes, and verify a private encrypted link.",
};

const topics = [
  { en: "Start here", zh: "从这里开始", textEn: "What Anet solves and where its security boundary begins.", textZh: "Anet 解决什么问题，以及安全边界从哪里开始。", href: "#introduction", n: "01" },
  { en: "Architecture", zh: "架构", textEn: "Identity, packets, transports, stores, acknowledgements, and routing.", textZh: "身份、Packet、传输、存储、确认与路由。", href: "#architecture", n: "02" },
  { en: "Operations", zh: "运维", textEn: "Node lifecycle, pairing, recovery, release gates, and diagnostics.", textZh: "节点生命周期、配对、恢复、发布门禁和诊断。", href: "#operations", n: "03" },
  { en: "Integrations", zh: "集成", textEn: "Use the CLI as control plane and scoped MCP as the data plane.", textZh: "使用 CLI 作为控制平面，用窄权限 MCP 作为数据平面。", href: "#integrations", n: "04" },
];

export default function Docs() {
  return (
    <main>
      <Header />
      <div className="docs-layout shell">
        <aside className="docs-nav">
          <span><T en="DOCUMENTATION" zh="文档" /></span>
          <a href="#introduction"><T en="Introduction" zh="简介" /></a>
          <a href="#getting-started"><T en="Quickstart" zh="快速开始" /></a>
          <a href="#architecture"><T en="Architecture" zh="架构" /></a>
          <a href="#security"><T en="Security" zh="安全" /></a>
          <a href="#operations"><T en="Operations" zh="运维" /></a>
          <a href="#integrations"><T en="Integrations" zh="集成" /></a>
          <a href="#contributing"><T en="Contributing" zh="参与贡献" /></a>
          <small><T en="Generated engineering reference is maintained by OpenWiki in the source repository." zh="工程参考文档由源码仓库中的 OpenWiki 持续生成。" /></small>
        </aside>
        <article className="docs-content">
          <div className="docs-hero" id="introduction">
            <p className="section-label"><T en="ANET DOCUMENTATION" zh="ANET 文档" /></p>
            <h1><T en={<>Build private links<br />between independent nodes.</>} zh={<>在独立节点之间<br />建立私有连接。</>} /></h1>
            <p>
              <T en="This guide is the stable entrance to Anet. The detailed engineering wiki is generated from the repository, so protocol and operational documentation stay close to implementation." zh="这里是 Anet 的稳定文档入口。详细工程 Wiki 直接从仓库生成，使协议与运维文档始终贴近实现。" />
            </p>
          </div>
          <div className="topic-grid">
            {topics.map((topic) => (
              <a href={topic.href} className="topic-card" key={topic.en}>
                <span>{topic.n}</span><strong><T en={topic.en} zh={topic.zh} /></strong><p><T en={topic.textEn} zh={topic.textZh} /></p><em><T en="Read section →" zh="阅读本节 →" /></em>
              </a>
            ))}
          </div>
          <section className="doc-section" id="getting-started">
            <p className="section-label"><T en="QUICKSTART" zh="快速开始" /></p>
            <h2><T en="Two nodes. Two homes. One explicit trust relationship." zh="两个节点。两个私有目录。一段明确的信任关系。" /></h2>
            <div className="notice"><b><T en="Private state stays private. " zh="私有状态必须保持私有。" /></b><T en={<>Never copy <code>identity.json</code>, <code>tls-key.pem</code>, SQLite state, or an entire node home between runtimes.</>} zh={<>绝不要在不同 runtime 之间复制 <code>identity.json</code>、<code>tls-key.pem</code>、SQLite 状态或整个节点目录。</>} /></div>
            <div className="code-block">
              <div><span>POWERSHELL</span><small><T en="local demonstration" zh="本机演示" /></small></div>
              <pre><code>{`python -m pip install -e .

anet --home .\\demo\\a init --label node_a --host 127.0.0.1 --port 43101
anet --home .\\demo\\b init --label node_b --host 127.0.0.1 --port 43102

anet --home .\\demo\\a card --out .\\demo\\a.card.json
anet --home .\\demo\\b card --out .\\demo\\b.card.json

anet --home .\\demo\\a peer-add .\\demo\\b.card.json
anet --home .\\demo\\b peer-add .\\demo\\a.card.json`}</code></pre>
            </div>
            <p className="doc-note"><T en="Loopback is only for a same-host demo. Physical devices need reachable LAN addresses, distinct ports, distinct Node IDs, and distinct private homes." zh="回环地址只适合单机演示。物理设备需要可达的局域网地址、不同端口、不同 Node ID 和各自独立的私有目录。" /></p>
          </section>

          <section className="doc-section" id="architecture">
            <p className="section-label"><T en="ARCHITECTURE" zh="架构" /></p>
            <h2><T en="A small set of boundaries that stay true." zh="少数始终成立的边界。" /></h2>
            <div className="definition-list">
              <div><b><T en="Identity" zh="身份" /></b><p><T en="A complete cryptographic Node ID, not a label, profile, IP address, or platform account." zh="完整的加密 Node ID，而不是标签、profile、IP 地址或平台账号。" /></p></div>
              <div><b>Packet</b><p><T en="An immutable sealed object with visible routing metadata and encrypted sender identity and payload." zh="不可变的密封对象；路由元数据可见，发送者身份与载荷保持加密。" /></p></div>
              <div><b><T en="Transport" zh="传输" /></b><p><T en="Direct TLS, asynchronous carriers, relays, or offline bundles move the same packet." zh="TLS 直连、异步 Carrier、中继或离线 Bundle 都搬运同一个 Packet。" /></p></div>
              <div><b><T en="Store" zh="存储" /></b><p><T en="Local SQLite persistence tracks queues, inbox, leases, receipts, prekeys, path health, and deduplication." zh="本地 SQLite 持久化队列、Inbox、租约、回执、预密钥、路径健康与去重状态。" /></p></div>
            </div>
          </section>

          <section className="doc-section" id="security">
            <p className="section-label"><T en="SECURITY" zh="安全" /></p>
            <h2><T en="Authenticated input is still input." zh="通过认证的输入，依然只是输入。" /></h2>
            <p><T en="Anet establishes who signed an object. Local policy still decides whether that principal may cause an effect. Persistent identity creation, trust changes, and service registration remain explicit operator actions." zh="Anet 确认对象由谁签名。本地策略仍要决定该主体是否可以产生实际效果。创建持久身份、修改信任和注册服务始终需要操作者明确授权。" /></p>
            <div className="boundary-grid">
              <div><span><T en="PUBLIC" zh="公开" /></span><strong><T en="Signed Peer Card" zh="签名 Peer Card" /></strong><p><T en="Safe to exchange for review and explicit pinning." zh="可以安全交换，用于审核与显式固定。" /></p></div>
              <div><span><T en="PRIVATE" zh="私有" /></span><strong><T en="Node home" zh="节点目录" /></strong><p><T en="Identity keys, TLS key, database, configuration, and runtime state." zh="身份密钥、TLS 密钥、数据库、配置与 runtime 状态。" /></p></div>
              <div><span><T en="LOCAL" zh="本地" /></span><strong><T en="Authorization" zh="授权" /></strong><p><T en="Capabilities, allowed peers, durable consumer scope, and approval." zh="Capability、允许的 Peer、持久 Consumer 范围与审批。" /></p></div>
            </div>
          </section>

          <section className="doc-section two-col-doc" id="operations">
            <div><p className="section-label"><T en="OPERATIONS" zh="运维" /></p><h2><T en="Lifecycle before convenience." zh="生命周期先于便利。" /></h2></div>
            <ul>
              <li><T en="Locate the deployment-owned home before repair." zh="修复前先定位部署实际拥有的节点目录。" /></li>
              <li><T en="Use signed cards and challenge-response pairing." zh="使用签名 Card 与挑战响应配对。" /></li>
              <li><T en="Change addresses through signed locator configuration." zh="通过签名 Locator 配置修改地址。" /></li>
              <li><T en="Back up and restore only within one runtime’s ownership boundary." zh="仅在同一个 runtime 的所有权边界内备份与恢复。" /></li>
              <li><T en="Revoke locally with the complete Node ID." zh="使用完整 Node ID 执行本地撤销。" /></li>
            </ul>
          </section>

          <section className="doc-section two-col-doc" id="integrations">
            <div><p className="section-label"><T en="INTEGRATIONS" zh="集成" /></p><h2><T en="Give agents the narrowest useful surface." zh="只给 Agent 最窄且够用的能力面。" /></h2></div>
            <p><T en="Use the CLI for sparse install, identity, trust, configuration, diagnostics, and recovery. Use a scoped stdio MCP session for repeated messaging, durable claims, and typed tasks. Ephemeral workers receive no persistent Anet node by default." zh="安装、身份、信任、配置、诊断与恢复使用 CLI；重复消息、持久 Claim 与类型化任务使用受限的 stdio MCP。临时 Worker 默认不获得持久 Anet 节点。" /></p>
          </section>

          <section className="doc-section doc-cta" id="contributing">
            <div><p className="section-label"><T en="CONTRIBUTING" zh="参与贡献" /></p><h2><T en="Documentation lives with the code." zh="文档与代码共同存在。" /></h2><p><T en="OpenWiki generates the recurring engineering reference. Change source and reviewed docs together; do not create a second truth in a detached wiki." zh="OpenWiki 持续生成工程参考文档。源码与受审文档应一起修改，不要在独立 Wiki 中制造第二套真相。" /></p></div>
            <Link className="button primary" href="/#install"><T en="Install with your agent" zh="让你的 Agent 安装" /> <span>→</span></Link>
          </section>
        </article>
      </div>
      <Footer />
    </main>
  );
}
