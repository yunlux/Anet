import Link from "next/link";

export function Header() {
  return (
    <header className="site-header">
      <div className="shell header-inner">
        <Link className="brand" href="/" aria-label="Anet home">
          <span className="brand-mark"><i /><i /><i /></span>
          <span>ANET</span>
        </Link>
        <nav aria-label="Primary navigation">
          <Link href="/docs">Docs</Link>
          <Link href="/download">Download</Link>
          <Link href="/blog">Updates</Link>
        </nav>
        <Link className="header-cta" href="/docs#getting-started">Get started <span>↗</span></Link>
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
          <p>Private infrastructure for agent networks.</p>
        </div>
        <div><strong>Explore</strong><Link href="/docs">Documentation</Link><Link href="/download">Install</Link><Link href="/blog">Updates</Link></div>
        <div><strong>Project</strong><Link href="/docs#security">Security</Link><Link href="/docs#architecture">Architecture</Link><Link href="/docs#contributing">Contributing</Link></div>
        <div className="footer-status"><span className="pulse" /> v0.12.1 · active development</div>
      </div>
      <div className="shell footer-bottom"><span>Apache License 2.0</span><span>Built for explicit trust.</span></div>
    </footer>
  );
}
