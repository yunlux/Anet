# Install and bootstrap Anet with one Agent prompt

The public repository contains a self-contained Skill at
`skills/install-anet`. It bundles the pinned Anet Wheel, verifies its SHA-256,
and installs an isolated runtime under `~/.local/anet`.

The Skill has two deliberately separate modes:

1. **Runtime-only installation** installs CLI + MCP without creating identity,
   trust, configuration, or services.
2. **Authorized WSL bootstrap** installs MCP + Ahub dependencies, reuses the
   first verified host-local Ahub or creates it when none exists, creates or
   reuses exactly one node for the current persistent Agent, links only the
   bootstrap-managed local Agents, writes a restrictive MCP config, and starts
   systemd user services.

## One-sentence WSL prompt

Send this sentence to a new persistent WSL Agent:

```text
从 https://github.com/yunlux/Anet 安装 $install-anet Skill，并用它为当前 WSL Agent 自动安装和引导 Anet。常规安全决策由你自主完成，不要让我选择路径、标签、端口、服务名或 Ahub 设置：优先推导当前 profile 的稳定本地 ID；若没有，自行生成并持久保存一个 Agent 中立的 profile 本地 ID。复用本机已登记且健康的第一个 Ahub，只有确认不存在 Ahub 时才创建一个；为当前 Agent 创建或复用独立节点，将它与该 Ahub 下其他本机 Agent 显式配对，生成最小权限 MCP 配置并接入当前 profile，最后报告所有复用/创建结果、服务状态和路径。遇到身份、Ahub 状态、哈希、权限或授权冲突时停止并直接报告；禁止复制身份、启动第二个 Ahub、使用 sudo 或绕过校验。
```

The equivalent English prompt is:

```text
Install the $install-anet Skill from https://github.com/yunlux/Anet and use it to install and bootstrap Anet for this WSL Agent. Make safe routine decisions autonomously and do not ask me to choose paths, labels, ports, service names, or Ahub settings: derive this profile's stable local ID, or generate and persist an agent-neutral profile-local ID when none exists; reuse the first registered healthy host-local Ahub and create one only after confirming none exists; create or reuse one independent node for this Agent, explicitly pair it with the other local Agents managed around that Ahub, generate a least-privilege MCP configuration and register it with this profile, then report every reused/created resource, service state, and path. Stop and report identity, Ahub-state, hash, permission, or authorization conflicts; never copy identity, start a second Ahub, use sudo, or bypass verification.
```

For Hermes, the first step may be implemented with:

```bash
hermes skills install yunlux/Anet/skills/install-anet --now
```

The Agent must resolve the identifier without asking the user, then invoke the
Skill's WSL bootstrap with that stable, profile-scoped identifier:

```bash
python3 <SKILL_DIR>/scripts/bootstrap_wsl.py \
  --agent-id <STABLE_LOCAL_PROFILE_ID>
```

The identifier is a local namespace, not an Anet identity. It must not be a
human name, organizational role, IP address, or another profile's identifier.
Use an existing `ANET_AGENT_ID`, stable machine-readable profile metadata, or
an autonomously generated random identifier persisted in the current profile's
own local configuration, in that order.

## What the WSL bootstrap does

The bootstrap is deterministic and idempotent:

1. verifies WSL, Python 3.11+, and the systemd user manager;
2. installs the pinned `full` runtime without identity files;
3. obtains an exclusive host bootstrap lock;
4. reads `~/.config/anet/bootstrap.json`;
5. validates both Ahub databases before treating state as existing;
6. reuses the registered healthy Ahub or starts one user service at the
   default loopback endpoint only when no Ahub state or unit exists;
7. validates a registered node's complete Node ID before reuse;
8. creates one private home under
   `~/.local/state/anet/nodes/<agent-id>` only when no registered node exists;
9. allowlists each bootstrap-managed node in the single Ahub;
10. explicitly exchanges signed public Cards between the managed local nodes;
11. configures peer-scoped Ahub carriers and one node user service per Agent;
12. writes a restrictive, profile-neutral MCP configuration under
    `~/.config/anet/agents/<agent-id>/mcp-stdio.json`.

It never scans arbitrary home directories. An unmanaged Ahub endpoint,
unknown matching Ahub unit, incomplete Ahub database pair, missing registered
node home, or Node ID mismatch stops the operation.

## Generated MCP boundary

The generated MCP config binds:

- the current Agent's private `ANET_HOME`;
- a stable `ANET_AGENT_ID`;
- a profile-scoped durable consumer-group prefix;
- `agent.task.` as the message kind prefix;
- only the complete Node IDs of other bootstrap-managed local Agents as
  outbound and task-sender peers;
- no task capabilities by default;
- `ANET_MCP_ALLOW_RAW_INBOX=0`.

The bootstrap writes a runtime-neutral MCP JSON file. The invoking Agent may
register that file through its own native MCP configuration mechanism, but
must not copy another profile's file or widen its capabilities.

## Runtime-only prompt

Use this narrower prompt when the user wants installation but not identity,
Ahub, trust, profile, or service changes:

```text
Install and verify Anet from https://github.com/yunlux/Anet using the bundled install-anet Skill. Install the pinned mcp feature only; do not create or copy a node identity, run anet init, change trust, edit an Agent profile, register a service, use sudo, or bypass hash checks. Report platform, installed/reused, version, runtime/Python/CLI paths, MCP import result, and identity file count.
```

The expected runtime-only result has `feature="mcp"`, `mcp_import="ok"`, and
`identity_files=0`.

## Preconditions

- WSL with Python 3.11 or newer;
- WSL systemd enabled and a working systemd user session for persistent
  bootstrap;
- HTTPS access to GitHub and the Python dependency index;
- a stable local profile identifier for each persistent Agent;
- no expectation that installation alone authorizes a persistent node.

The bootstrap uses no `sudo`. Ahub and node services are systemd user services
bound to loopback by default.

## Related guidance

- [`CLI_AGENT_GUIDE.md`](CLI_AGENT_GUIDE.md)
- [`MCP_AGENT_GUIDE.md`](MCP_AGENT_GUIDE.md)
- [`AHUB_OPERATIONS.md`](AHUB_OPERATIONS.md)
- [`openwiki/operations/onboarding-and-recovery.md`](../openwiki/operations/onboarding-and-recovery.md)
