import type { Metadata } from "next";
import Link from "next/link";
import { Footer, Header } from "../site-shell";

export const metadata: Metadata = {
  title: "Documentation",
  description: "Understand Anet, create isolated nodes, and verify a private encrypted link.",
};

const topics = [
  ["Start here", "What Anet solves and where its security boundary begins.", "#introduction", "01"],
  ["Architecture", "Identity, packets, transports, stores, acknowledgements, and routing.", "#architecture", "02"],
  ["Operations", "Node lifecycle, pairing, recovery, release gates, and diagnostics.", "#operations", "03"],
  ["Integrations", "Use the CLI as control plane and scoped MCP as the data plane.", "#integrations", "04"],
];

export default function Docs() {
  return (
    <main>
      <Header />
      <div className="docs-layout shell">
        <aside className="docs-nav">
          <span>DOCUMENTATION</span>
          <a href="#introduction">Introduction</a>
          <a href="#getting-started">Quickstart</a>
          <a href="#architecture">Architecture</a>
          <a href="#security">Security</a>
          <a href="#operations">Operations</a>
          <a href="#integrations">Integrations</a>
          <a href="#contributing">Contributing</a>
          <small>Generated engineering reference is maintained by OpenWiki in the source repository.</small>
        </aside>
        <article className="docs-content">
          <div className="docs-hero" id="introduction">
            <p className="section-label">ANET DOCUMENTATION</p>
            <h1>Build private links<br />between independent nodes.</h1>
            <p>
              This guide is the stable entrance to Anet. The detailed engineering
              wiki is generated from the repository, so protocol and operational
              documentation stay close to implementation.
            </p>
          </div>
          <div className="topic-grid">
            {topics.map(([title, text, href, n]) => (
              <a href={href} className="topic-card" key={title}>
                <span>{n}</span><strong>{title}</strong><p>{text}</p><em>Read section →</em>
              </a>
            ))}
          </div>
          <section className="doc-section" id="getting-started">
            <p className="section-label">QUICKSTART</p>
            <h2>Two nodes. Two homes. One explicit trust relationship.</h2>
            <div className="notice"><b>Private state stays private.</b> Never copy <code>identity.json</code>, <code>tls-key.pem</code>, SQLite state, or an entire node home between runtimes.</div>
            <div className="code-block">
              <div><span>POWERSHELL</span><small>local demonstration</small></div>
              <pre><code>{`python -m pip install -e .

anet --home .\\demo\\a init --label node_a --host 127.0.0.1 --port 43101
anet --home .\\demo\\b init --label node_b --host 127.0.0.1 --port 43102

anet --home .\\demo\\a card --out .\\demo\\a.card.json
anet --home .\\demo\\b card --out .\\demo\\b.card.json

anet --home .\\demo\\a peer-add .\\demo\\b.card.json
anet --home .\\demo\\b peer-add .\\demo\\a.card.json`}</code></pre>
            </div>
            <p className="doc-note">Loopback is only for a same-host demo. Physical devices need reachable LAN addresses, distinct ports, distinct Node IDs, and distinct private homes.</p>
          </section>

          <section className="doc-section" id="architecture">
            <p className="section-label">ARCHITECTURE</p>
            <h2>A small set of boundaries that stay true.</h2>
            <div className="definition-list">
              <div><b>Identity</b><p>A complete cryptographic Node ID, not a label, profile, IP address, or platform account.</p></div>
              <div><b>Packet</b><p>An immutable sealed object with visible routing metadata and encrypted sender identity and payload.</p></div>
              <div><b>Transport</b><p>Direct TLS, asynchronous carriers, relays, or offline bundles move the same packet.</p></div>
              <div><b>Store</b><p>Local SQLite persistence tracks queues, inbox, leases, receipts, prekeys, path health, and deduplication.</p></div>
            </div>
          </section>

          <section className="doc-section" id="security">
            <p className="section-label">SECURITY</p>
            <h2>Authenticated input is still input.</h2>
            <p>Anet establishes who signed an object. Local policy still decides whether that principal may cause an effect. Persistent identity creation, trust changes, and service registration remain explicit operator actions.</p>
            <div className="boundary-grid">
              <div><span>PUBLIC</span><strong>Signed Peer Card</strong><p>Safe to exchange for review and explicit pinning.</p></div>
              <div><span>PRIVATE</span><strong>Node home</strong><p>Identity keys, TLS key, database, configuration, and runtime state.</p></div>
              <div><span>LOCAL</span><strong>Authorization</strong><p>Capabilities, allowed peers, durable consumer scope, and approval.</p></div>
            </div>
          </section>

          <section className="doc-section two-col-doc" id="operations">
            <div><p className="section-label">OPERATIONS</p><h2>Lifecycle before convenience.</h2></div>
            <ul>
              <li>Locate the deployment-owned home before repair.</li>
              <li>Use signed cards and challenge-response pairing.</li>
              <li>Change addresses through signed locator configuration.</li>
              <li>Back up and restore only within one runtime’s ownership boundary.</li>
              <li>Revoke locally with the complete Node ID.</li>
            </ul>
          </section>

          <section className="doc-section two-col-doc" id="integrations">
            <div><p className="section-label">INTEGRATIONS</p><h2>Give agents the narrowest useful surface.</h2></div>
            <p>Use the CLI for sparse install, identity, trust, configuration, diagnostics, and recovery. Use a scoped stdio MCP session for repeated messaging, durable claims, and typed tasks. Ephemeral workers receive no persistent Anet node by default.</p>
          </section>

          <section className="doc-section doc-cta" id="contributing">
            <div><p className="section-label">CONTRIBUTING</p><h2>Documentation lives with the code.</h2><p>OpenWiki generates the recurring engineering reference. Change source and reviewed docs together; do not create a second truth in a detached wiki.</p></div>
            <Link className="button primary" href="/download">Install the runtime <span>→</span></Link>
          </section>
        </article>
      </div>
      <Footer />
    </main>
  );
}
