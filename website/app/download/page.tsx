import type { Metadata } from "next";
import Link from "next/link";
import { Footer, Header } from "../site-shell";

export const metadata: Metadata = {
  title: "Download",
  description: "Install the Anet runtime on Windows, WSL, macOS, or Linux.",
};

const platforms = [
  ["Windows", "PowerShell", "scripts/install_windows.ps1", "Native runtime installer"],
  ["WSL / Linux", "Python", "scripts/install_wsl.py", "Isolated Linux runtime"],
  ["macOS", "Python", "scripts/install_macos.py", "Native macOS runtime"],
];

export default function Download() {
  return (
    <main>
      <Header />
      <section className="page-hero shell">
        <p className="section-label">INSTALL ANET</p>
        <h1>Install the runtime.<br />Own the node separately.</h1>
        <p>An installer puts versioned Anet software on a machine. It does not create an identity, copy trust, read an agent profile, or register a persistent service.</p>
      </section>
      <section className="platforms shell">
        {platforms.map(([name, shell, command, note], index) => (
          <article className="platform-card" key={name}>
            <span>0{index + 1}</span><small>{shell}</small><h2>{name}</h2><p>{note}</p>
            <div className="mini-code">{command}</div>
          </article>
        ))}
      </section>
      <section className="install-flow shell section">
        <div><p className="section-label">SAFE SEQUENCE</p><h2>Software first.<br />Identity second.</h2></div>
        <ol>
          <li><b>01</b><span><strong>Install</strong>Verify the CLI and exact version.</span></li>
          <li><b>02</b><span><strong>Choose ownership</strong>Select one private home for one persistent runtime.</span></li>
          <li><b>03</b><span><strong>Initialize</strong>Create a new node only with explicit operator intent.</span></li>
          <li><b>04</b><span><strong>Pair</strong>Review signed public cards and pin trust deliberately.</span></li>
        </ol>
      </section>
      <section className="download-cta shell section">
        <h2>Need the complete command path?</h2>
        <Link className="button primary" href="/docs#getting-started">Open the quickstart <span>→</span></Link>
      </section>
      <Footer />
    </main>
  );
}
