import type { ReactNode } from "react";
import Link from "next/link";
import { LanguageToggle } from "./language-controls";

export function T({ en, zh }: { en: ReactNode; zh: ReactNode }) {
  return <><span className="lang-en">{en}</span><span className="lang-zh">{zh}</span></>;
}

export function Header() {
  return (
    <header className="site-header">
      <div className="shell header-inner">
        <Link className="brand" href="/" aria-label="Anet home">
          <span className="brand-mark"><i /><i /><i /></span>
          <span>ANET</span>
        </Link>
        <nav aria-label="Primary navigation">
          <Link href="/docs"><T en="Docs" zh="文档" /></Link>
          <Link href="/#install"><T en="Install" zh="安装" /></Link>
          <Link href="/blog"><T en="Updates" zh="更新" /></Link>
          <Link href="/amesh"><T en="Amesh" zh="Amesh" /></Link>
          <a className="github-nav" href="https://github.com/yunlux/Anet" target="_blank" rel="noreferrer">
            <img src="https://github.githubassets.com/favicons/favicon.svg" alt="" aria-hidden="true" />
            GitHub
          </a>
        </nav>
        <div className="header-actions">
          <LanguageToggle />
          <Link className="header-cta" href="/#install"><T en="Get Anet" zh="获取 Anet" /> <span>↗</span></Link>
        </div>
      </div>
    </header>
  );
}

export function Footer() {
  return (
    <footer>
      <div className="shell footer-grid">
        <div>
          <Link className="brand footer-brand" href="/">
            <span className="brand-mark"><i /><i /><i /></span><span>ANET</span>
          </Link>
          <p><T en="Private infrastructure for agent networks." zh="面向 Agent 网络的私有基础设施。" /></p>
        </div>
        <div><strong><T en="Explore" zh="浏览" /></strong><Link href="/docs"><T en="Documentation" zh="文档" /></Link><Link href="/#install"><T en="Install" zh="安装" /></Link><Link href="/blog"><T en="Updates" zh="更新" /></Link></div>
        <div><strong><T en="Project" zh="项目" /></strong><a href="https://github.com/yunlux/Anet" target="_blank" rel="noreferrer">GitHub · yunlux/Anet</a><Link href="/docs#security"><T en="Security" zh="安全" /></Link><Link href="/docs#architecture"><T en="Architecture" zh="架构" /></Link></div>
        <div className="footer-status"><span className="pulse" /> <T en="v0.12.1 · active development" zh="v0.12.1 · 持续开发中" /></div>
      </div>
      <div className="shell footer-bottom"><span>Apache License 2.0</span><span><T en="Built for explicit trust." zh="为显式信任而构建。" /></span></div>
    </footer>
  );
}
