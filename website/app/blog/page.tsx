import type { Metadata } from "next";
import Link from "next/link";
import { Footer, Header } from "../site-shell";

export const metadata: Metadata = {
  title: "Updates",
  description: "Anet release notes and engineering updates.",
};

const releases = [
  ["v0.12.1", "Runtime installation without identity side effects", "Clean, agent-neutral installers for Windows, WSL, and macOS keep software installation separate from node creation, trust, and services.", "CURRENT"],
  ["v0.11.0", "Reliable bytes, independent of the path", "A shell-free stdio dialer lets serial links, radios, SSH pipes, and custom transports carry the existing authenticated Anet session.", "TRANSPORT"],
  ["v0.10.0", "Bounded replication across carriers", "The same immutable ciphertext can travel over multiple independent store-and-forward paths while destination acknowledgements converge.", "ROUTING"],
  ["v0.9.0", "Health checks without business traffic", "Authenticated path probes classify failures without moving packets, creating inbox state, or distorting normal route scores.", "OPERATIONS"],
];

export default function Blog() {
  return (
    <main>
      <Header />
      <section className="page-hero shell">
        <p className="section-label">ENGINEERING UPDATES</p>
        <h1>Progress measured<br />at the boundary.</h1>
        <p>Anet releases focus on verifiable identity isolation, durable delivery semantics, replaceable transports, and safe operations.</p>
      </section>
      <section className="release-list shell">
        {releases.map(([version, title, text, tag]) => (
          <article key={version}>
            <div><span className="release-version">{version}</span><small>{tag}</small></div>
            <div><h2>{title}</h2><p>{text}</p></div>
            <span className="release-arrow">↗</span>
          </article>
        ))}
      </section>
      <section className="updates-note shell section">
        <div><p className="section-label">SOURCE OF TRUTH</p><h2>Release notes belong beside the implementation.</h2></div>
        <div><p>The repository changelog carries the complete technical record. This page stays intentionally concise: what changed, which boundary it affects, and why it matters.</p><Link href="/docs">Read the architecture <span>→</span></Link></div>
      </section>
      <Footer />
    </main>
  );
}
