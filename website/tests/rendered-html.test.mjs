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
  assert.match(html, /WINDOWS.*WSL.*MACOS.*LINUX.*TERMUX/s);
  assert.match(html, /ONE-CLICK DEPLOYMENT/);
  assert.match(html, /https:\/\/github\.com\/yunlux\/Anet/);
  assert.match(html, /scripts\/install_windows_oneclick\.ps1/);
  assert.match(html, /matching install_\*_oneclick\.py/);
  assert.match(html, /bounded duplicate preflight/);
  assert.match(html, /target-scoped install lock/);
  assert.match(html, /software\.wheel_url/);
  assert.match(html, /software\.repo_url/);
  assert.match(html, /repo_ref/);
  assert.match(html, /Git source/);
  assert.match(html, /Git 源/);
  assert.match(html, /用户已授权创建一个独立持久节点/);
  assert.match(html, /Windows and WSL remain separate nodes/);
  assert.match(html, /Windows 与 WSL 即使使用镜像网络也必须是不同/);
  assert.match(html, /Never copy another device/);
  assert.match(html, /禁止复制其他设备的 identity/);
  assert.match(html, /TLS 1\.3 \/ signed challenge/);
  assert.match(html, /SQLite WAL \/ durable queue/);
  assert.match(html, /-Admin/);
  assert.match(html, /&lt;CONTROL_URL&gt;/);
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
    ["/agent-social", "Agents have"],
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
  assert.match(html, /仪表板/);
  assert.match(html, /导入报告视图/);
  assert.match(html, /relation-reported-view/);
  assert.match(html, /Actor-to-Actor relationship claim/);
  assert.match(html, /RELATIONSHIP SUGGESTIONS/);
  assert.match(html, /只读候选/);
  assert.match(html, />采纳</);
  assert.match(html, />拒绝</);
  assert.match(html, /APPEND ORDER/);
  assert.match(html, /RELATIONSHIP DISCLOSURE/);
  assert.match(html, /OBSERVER-LOCAL DISCLOSURE SCHEDULE/);
  assert.match(html, /history replay: off/);
  assert.match(html, /NO AUDIENCE PULL/);
  assert.match(html, /VIEWPOINT WORKBENCH/);
  assert.match(html, /SENDER-REPORTED/);
  assert.match(html, /PROVEN-CONTINUOUS-SEGMENT/);
  assert.match(html, /CONTINUITY VERIFIED/);
  assert.match(html, /cursor links verified/);
  assert.match(html, /current-state-after-last-cursor-not-proven/);
  assert.match(html, /模拟缺页/);
  assert.match(html, /ADVISORY NOTICE/);
  assert.match(html, /requested_action: none/);
  assert.match(html, /active schedule required/);
  assert.match(html, /咨询性缺页通知/);
  assert.match(html, /MUTUAL RELATIONSHIP CLAIM \/ REVOCABLE/);
  assert.match(html, /共同声明可以撤回/);
  assert.match(html, /NO FORCED CIRCLE CHANGE/);
  assert.match(html, /G 的本地判断/);
  assert.match(html, /AUTHORIZATION EFFECT: NONE/);
  assert.match(html, /本地持久化顺序回放/);
  assert.match(html, /social-og-activity\.png/);
});

test("renders the agent-first network with a parent observer boundary", async () => {
  const response = await render("/agent-social");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /AGENT SOCIAL \/ PARENT OBSERVER MODE/);
  assert.match(html, /HUMAN PARENT \/ FAMILY VIEW/);
  assert.match(html, /READ ONLY/);
  assert.match(html, /social life/);
  assert.match(html, /CONVERSATION RECORD/);
  assert.match(html, /skill offered/);
  assert.match(html, /Parent observation is a relationship/);
  assert.match(html, /cannot mutate A/);
});

test("removes the standalone install route", async () => {
  const response = await render("/download");
  assert.equal(response.status, 404);
});
