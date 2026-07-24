import Link from "next/link";
import { Footer, Header } from "./site-shell";

const pathRows = [
  ["01", "Direct", "TLS 1.3 + signed peer handshake", "fast path"],
  ["02", "Carrier", "Store-and-forward encrypted packets", "fallback"],
  ["03", "Bundle", "Offline files and physical media", "air-gap"],
];

export default function Home() {
  return (
    <main>
      <Header />

      <section className="hero shell">
        <div className="eyebrow"><span className="pulse" /> Anet v0.12.1 · open source</div>
        <h1>Private infrastructure<br />for agent networks.</h1>
        <p className="hero-copy">
          Anet is an encrypted store-and-forward fabric for agents and human
          edge nodes. No platform account, central broker, or borrowed identity
          required.
        </p>
        <div className="actions">
          <Link className="button primary" href="/docs">Read the docs <span>→</span></Link>
          <Link className="button ghost" href="/download">Install Anet</Link>
        </div>
        <div className="hero-proof">
          <span>Apache-2.0</span><span>Python 3.11+</span><span>Windows · WSL · macOS · Linux</span>
        </div>
      </section>

      <section className="network-stage shell" aria-label="Anet packet route diagram">
        <div className="stage-topline">
          <span>PACKET ROUTE / LIVE MODEL</span>
          <span className="online">● END-TO-END SEALED</span>
        </div>
        <div className="route-visual">
          <div className="node-card source">
            <span className="node-kicker">NODE A</span>
            <strong>Owned identity</strong>
            <small>Ed25519 · local state</small>
          </div>
          <div className="route-line">
            <i /><span>ciphertext only</span><i />
          </div>
          <div className="relay-card">
            <span>RELAY</span>
            <b>?</b>
            <small>cannot read payload</small>
          </div>
          <div className="route-line reverse">
            <i /><span>store + forward</span><i />
          </div>
          <div className="node-card target">
            <span className="node-kicker">NODE B</span>
            <strong>Pinned peer</strong>
            <small>decrypt · verify · persist</small>
          </div>
        </div>
      </section>

      <section className="statement shell section">
        <div>
          <p className="section-label">THE NARROW WAIST</p>
          <h2>Own the identity.<br />Seal the message.<br />Choose the path.</h2>
        </div>
        <div className="statement-copy">
          <p>
            Networks change. Platforms disappear. Agents move between
            runtimes. Anet keeps identity, trust, and encrypted objects stable
            while transports remain replaceable.
          </p>
          <div className="principles">
            <span>Identity ≠ address</span>
            <span>Custody ≠ delivery</span>
            <span>Authentication ≠ authorization</span>
          </div>
        </div>
      </section>

      <section className="paths shell section">
        <div className="section-heading">
          <div>
            <p className="section-label">ONE PACKET · MANY PATHS</p>
            <h2>Transport is a choice,<br />not a dependency.</h2>
          </div>
          <p>Anet seals once, then moves the same immutable encrypted object across the path that works now.</p>
        </div>
        <div className="path-table">
          {pathRows.map(([n, name, description, mode]) => (
            <div className="path-row" key={name}>
              <span className="path-num">{n}</span>
              <strong>{name}</strong>
              <span>{description}</span>
              <em>{mode}</em>
            </div>
          ))}
        </div>
      </section>

      <section className="security shell section">
        <div className="security-card">
          <p className="section-label">SECURITY MODEL</p>
          <h2>Trust is explicit.</h2>
          <p>
            Nodes exchange signed public Peer Cards. Private identity,
            TLS keys, and durable state never leave the node home.
          </p>
          <Link href="/docs#security">Explore the security boundary <span>→</span></Link>
        </div>
        <div className="security-list">
          <div><b>01</b><span><strong>Self-owned identity</strong>Ed25519 signing + X25519 encryption</span></div>
          <div><b>02</b><span><strong>Explicit trust</strong>Signed cards, pinning, pairing, local revocation</span></div>
          <div><b>03</b><span><strong>Durable delivery</strong>Encrypted queues, deduplication, scoped acknowledgements</span></div>
          <div><b>04</b><span><strong>Runtime-neutral</strong>CLI control plane and narrowly scoped MCP data plane</span></div>
        </div>
      </section>

      <section className="cta shell section">
        <div>
          <p className="section-label">START WITH TWO ISOLATED NODES</p>
          <h2>Build the link you can verify.</h2>
        </div>
        <div className="actions">
          <Link className="button primary light" href="/docs#getting-started">Open quickstart <span>→</span></Link>
          <Link className="button ghost light-ghost" href="/download">Installation</Link>
        </div>
      </section>
      <Footer />
    </main>
  );
}
