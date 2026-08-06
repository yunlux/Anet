# Deployment Receipt v1

Every persistent one-click installer prints exactly one compact JSON object to
stdout after the node, control page, and native supervisor have been verified.
Human-readable preflight diagnostics remain on stderr. The common interface is:

```json
{
  "kind": "anet.deployment.receipt",
  "schema_version": 1,
  "ok": true,
  "outcome": "created",
  "platform": "wsl",
  "runtime": {
    "outcome": "installed",
    "platform": "wsl",
    "version": "0.13.1",
    "feature": "mcp",
    "runtime": "<RUNTIME>",
    "cli": "<CLI>"
  },
  "node": {
    "home": "<ANET_HOME>",
    "node_id": "<NODE_ID>",
    "listen_host": "127.0.0.1",
    "port": 4242,
    "advertise": [],
    "locator_contexts": []
  },
  "control": {
    "url": "<CONTROL_URL>",
    "key_id": "community-main",
    "key_ids": ["community-main", "actor-a"],
    "verified": true
  },
  "supervisor": {
    "kind": "systemd-user",
    "name": "anet-supervisor.service",
    "state": "active",
    "autostart": true,
    "health": {
      "kind": "anet.supervisor.health",
      "schema_version": 1,
      "ok": true,
      "state": "running",
      "fresh": true,
      "instance_id": "<SUPERVISOR_INSTANCE_ID>",
      "boot_session_id": "<BOOT_SESSION_ID>",
      "sync_complete": true,
      "supervisor_process_alive": true,
      "child_process_alive": true
    }
  },
  "preflight": {}
}
```

`outcome` is `created` for a new node and `reused` for the same verified node.
`platform` is one of `windows`, `wsl`, `linux`, `macos`, or `termux`.
`runtime` retains the versioned runtime install result. `supervisor` hides the
Scheduled Task, systemd, launchd, and runit differences behind one interface;
platform-specific paths or prerequisites may appear under `supervisor` or
`platform_details` without changing the required fields.

`control.verified: true` means the installer completed the read-only
`control-verify` gate before service registration. `supervisor.health` is read
through the installed CLI after service registration and requires a fresh
heartbeat plus live supervisor and `anet serve` child processes. It therefore
requires `sync_complete`, records the supervisor incarnation and OS boot
session, and proves the first persistent sync and child start observed during installation.
It does not prove survival of a later logout, reboot, network transition, or
operating-system policy change. Re-run `anet --home <ANET_HOME>
supervisor-status` for current evidence; see
[`SUPERVISOR_HEALTH_V1.md`](SUPERVISOR_HEALTH_V1.md).

`control.key_ids` lists every locally enrolled control publisher in installer
argument order. `control.key_id` remains the first item as a compatibility
field for existing v1 readers and identifies the locally pinned root-page
publisher. A legacy v1 receipt without `key_ids` remains valid and represents
either its single `key_id` or unsigned compatibility mode.

The receipt contains a complete Node ID, node-home path, control URL, local
service information, PIDs, and health timestamps. Treat it as private deployment evidence. Do not publish it
in issues, logs, screenshots, or Agent transcripts. Shared reports should use
redacted fingerprints and placeholders, following `VERIFICATION.md`.

Runtime-only installers do not emit this receipt because they intentionally do
not create a node or supervisor.
