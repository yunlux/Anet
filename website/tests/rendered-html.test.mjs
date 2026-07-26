import assert from "node:assert/strict";
import test from "node:test";

async function render(pathname = "/") {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);

  return worker.fetch(
    new Request(`http://localhost${pathname}`, {
      headers: { accept: "text/html" },
    }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("renders the Anet product homepage", async () => {
  const response = await render("/");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /Private infrastructure/);
  assert.match(html, /Own the identity/);
  assert.match(html, /Anet v0\.12\.1/);
  assert.match(html, /COPY → SEND TO YOUR AGENT/);
  assert.match(html, /复制 → 发送给你的 AGENT/);
  assert.match(html, /WINDOWS.*WSL.*MACOS.*LINUX/s);
  assert.match(html, /INSTALL-ANET/);
  assert.match(html, /https:\/\/github\.com\/yunlux\/Anet/);
  assert.match(html, /\$install-anet Skill/);
  assert.match(html, /Make safe routine decisions autonomously/);
  assert.match(html, /do not ask me to choose paths, labels, ports/);
  assert.match(html, /常规安全决策由你自主完成/);
  assert.match(html, /first registered healthy host-local Ahub/);
  assert.match(html, /never copy identity, start a second Ahub/);
  assert.match(html, /TLS 1\.3 \/ signed challenge/);
  assert.match(html, /SQLite WAL \/ durable queue/);
  assert.match(html, /install_windows\.ps1/);
  assert.match(html, /install_wsl\.py/);
  assert.match(html, /skills\/install-anet/);
  assert.doesNotMatch(html, /asks for its URL|询问地址/);
  assert.ok(
    html.indexOf('id="install"') < html.indexOf("network-stage"),
    "install module should appear before the network model",
  );
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

test("renders the complete public information architecture", async () => {
  for (const [path, expected] of [
    ["/docs", "Build private links"],
    ["/blog", "ABA-D0"],
    ["/social", "一个 Agent 眼中的"],
  ]) {
    const response = await render(path);
    assert.equal(response.status, 200, path);
    assert.match(await response.text(), new RegExp(expected, "i"), path);
  }
});

test("renders the relationship demo with explicit fact and inference boundaries", async () => {
  const response = await render("/social");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /SUBJECT 推测/);
  assert.match(html, /ACTOR 事实/);
  assert.match(html, /A 的关系圈层/);
  assert.match(html, /可验证事实/);
  assert.match(html, /关系推测/);
  assert.match(html, /不是平台确认的真实个体/);
  assert.match(html, /扫码添加好友/);
  assert.match(html, /导入本地模型/);
  assert.match(html, /Actor-to-Actor relationship claim/);
  assert.match(html, /RELATIONSHIP SUGGESTIONS/);
  assert.match(html, /只读候选/);
  assert.match(html, />采纳</);
  assert.match(html, />拒绝</);
  assert.match(html, /APPEND ORDER/);
  assert.match(html, /RELATIONSHIP DISCLOSURE/);
  assert.match(html, /SEPARATE LEDGER/);
  assert.match(html, /AUTHORIZATION EFFECT: NONE/);
  assert.match(html, /本地持久化顺序回放/);
  assert.match(html, /social-og-activity\.png/);
});

test("removes the standalone install route", async () => {
  const response = await render("/download");
  assert.equal(response.status, 404);
});
