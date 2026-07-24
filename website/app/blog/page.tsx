import type { Metadata } from "next";
import Link from "next/link";
import { Footer, Header, T } from "../site-shell";

export const metadata: Metadata = {
  title: "Updates",
  description: "Anet release notes and engineering updates.",
};

const releases = [
  { version: "v0.12.1", titleEn: "Runtime installation without identity side effects", titleZh: "不产生身份副作用的 Runtime 安装", textEn: "Clean, agent-neutral installers for Windows, WSL, and macOS keep software installation separate from node creation, trust, and services.", textZh: "面向 Windows、WSL 与 macOS 的 Agent 中立安装器，将软件安装与节点创建、信任和服务注册严格分开。", tagEn: "CURRENT", tagZh: "当前版本" },
  { version: "v0.11.0", titleEn: "Reliable bytes, independent of the path", titleZh: "可靠字节流，不依赖具体路径", textEn: "A shell-free stdio dialer lets serial links, radios, SSH pipes, and custom transports carry the existing authenticated Anet session.", textZh: "无 shell 的 stdio Dialer 让串口、无线电、SSH 管道和自定义传输承载现有的 Anet 认证会话。", tagEn: "TRANSPORT", tagZh: "传输" },
  { version: "v0.10.0", titleEn: "Bounded replication across carriers", titleZh: "跨 Carrier 的有界复制", textEn: "The same immutable ciphertext can travel over multiple independent store-and-forward paths while destination acknowledgements converge.", textZh: "同一份不可变密文可以沿多条独立存储转发路径移动，并最终收敛到目的端确认。", tagEn: "ROUTING", tagZh: "路由" },
  { version: "v0.9.0", titleEn: "Health checks without business traffic", titleZh: "不携带业务流量的健康检查", textEn: "Authenticated path probes classify failures without moving packets, creating inbox state, or distorting normal route scores.", textZh: "经过认证的路径探针在不搬运 Packet、不创建 Inbox 状态且不污染正常路由评分的前提下分类故障。", tagEn: "OPERATIONS", tagZh: "运维" },
];

export default function Blog() {
  return (
    <main>
      <Header />
      <section className="page-hero shell">
        <p className="section-label"><T en="ENGINEERING UPDATES" zh="工程更新" /></p>
        <h1><T en={<>Progress measured<br />at the boundary.</>} zh={<>用边界是否成立<br />衡量进展。</>} /></h1>
        <p><T en="Anet releases focus on verifiable identity isolation, durable delivery semantics, replaceable transports, and safe operations." zh="Anet 的版本演进聚焦于可验证的身份隔离、持久送达语义、可替换传输和安全运维。" /></p>
      </section>
      <section className="release-list shell">
        {releases.map((release) => (
          <article key={release.version}>
            <div><span className="release-version">{release.version}</span><small><T en={release.tagEn} zh={release.tagZh} /></small></div>
            <div><h2><T en={release.titleEn} zh={release.titleZh} /></h2><p><T en={release.textEn} zh={release.textZh} /></p></div>
            <span className="release-arrow">↗</span>
          </article>
        ))}
      </section>
      <section className="updates-note shell section">
        <div><p className="section-label"><T en="SOURCE OF TRUTH" zh="事实来源" /></p><h2><T en="Release notes belong beside the implementation." zh="发布记录应与实现放在一起。" /></h2></div>
        <div><p><T en="The repository changelog carries the complete technical record. This page stays intentionally concise: what changed, which boundary it affects, and why it matters." zh="仓库 Changelog 保留完整技术记录。这个页面刻意保持简洁：改了什么、影响哪条边界，以及为什么重要。" /></p><Link href="/docs"><T en="Read the architecture" zh="阅读架构文档" /> <span>→</span></Link></div>
      </section>
      <Footer />
    </main>
  );
}
