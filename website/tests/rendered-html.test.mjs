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
  assert.match(html, /Copy this/);
  assert.match(html, /发送给你的 Agent 即可安装/);
  assert.match(html, /INSTALL-ANET/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

test("renders the complete public information architecture", async () => {
  for (const [path, expected] of [
    ["/docs", "Build private links"],
    ["/blog", "Progress measured"],
  ]) {
    const response = await render(path);
    assert.equal(response.status, 200, path);
    assert.match(await response.text(), new RegExp(expected, "i"), path);
  }
});

test("removes the standalone install route", async () => {
  const response = await render("/download");
  assert.equal(response.status, 404);
});
