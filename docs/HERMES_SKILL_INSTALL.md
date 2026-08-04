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

## Full persistent one-click deployment prompt

Use this prompt only when the user has explicitly authorized creating a
persistent node, remote-control configuration, and service/autostart state on
the target device. It is the prompt for the new-device deployment path; the
Skill-only prompt below remains runtime-only unless WSL bootstrap is separately
authorized.

Chinese:

```text
从 https://github.com/yunlux/Anet 在本机完成 Anet 一键部署。用户已明确授权创建一个独立持久节点、服务/自启动，以及 supervisor 对这个控制页 URL 的轮询：<CONTROL_URL>（执行前替换占位符）。自动检测平台并使用对应的一键入口：原生 Windows 使用从 GitHub Raw 获取的 PowerShell 入口，只有需要整机开机启动且当前 PowerShell 已提升权限时才加入 -Admin；WSL、Linux、macOS、Termux 没有 checkout 时使用从 GitHub Raw 获取的 `bootstrap_posix.py` 管道并传入对应 `--platform`，已有 checkout 时才直接运行对应的 install_*_oneclick.py。让控制页提供软件版本，以及首次安装 `software.wheel_url` 或 `software.repo_url`（顶层 `repo_url` 也可以）。使用仓库地址时，可选的 `software.repo_ref`（或顶层 `repo_ref`）固定 Git 分支、tag 或 commit；如果页面已签名，wheel 首次安装和后续更新必须有有效的 `software.sha256`，签名 `repo_url` 才是源码安装替代路径；无签名兼容模式才可在缺少哈希时计算本地 wheel，设备通过 Git 源安装时需要有 Git。安装器让 pip 通过 Git 源安装，并让 supervisor 继续处理远程软件、默认配置、Peer Card、pages/kv 更新。安装器在注册持久服务前必须调用已安装 CLI 的只读 `control-verify`；它不能写入首次同步状态，签名、有效期、Peer Card 或网络策略校验失败就停止。安装前先取得按目标目录隔离的安装锁，再执行有界重复检测；发现已有 Anet/Ahub 部署、服务、任务或进程时停止，只有用户明确要求第二套部署时才使用 -AllowExisting/--allow-existing。Windows 与 WSL 即使使用镜像网络也必须是不同 node home、identity、Node ID 和监听端口；host-scoped 地址不能发布 127.0.0.1。若要求 WSL 在 Windows 重启后恢复，且主机侧注册已获授权，再注册 WSL keepalive 任务。最后验证并报告 runtime、node home、完整 Node ID、服务/任务状态、控制页 URL 和复用/创建结果。禁止复制其他设备的 identity、TLS 私钥、SQLite 状态或整个 node home；遇到身份、哈希、权限、控制页或授权冲突时停止并报告。
```

English:

```text
Deploy Anet on this device from https://github.com/yunlux/Anet. The user explicitly authorizes one independent persistent node, its service/autostart state, and supervisor polling of this control-page URL: <CONTROL_URL> (replace the placeholder before running). Detect the platform and use its one-click entry point: on native Windows fetch and run the PowerShell entry point, adding -Admin only from an elevated PowerShell when machine-wide boot startup is requested; on WSL, Linux, macOS, or Termux use the checkout-free `scripts/bootstrap_posix.py` pipe with the matching `--platform` value when no checkout is present, or the direct install_*_oneclick.py script from an existing checkout. Require the control page to provide the release version plus either an initial wheel or repo_url; when using repo_url, an optional repo_ref pins a Git branch, tag, or commit. For a signed page, require a valid software.sha256 for every wheel bootstrap/update; signed repo_url is the explicit source-install alternative. Only unsigned compatibility mode may compute a local wheel hash when the declaration is absent. Then let the supervisor apply later software, default-config, Peer Card, and pages/kv updates. Before registering a persistent service, require the installed CLI's read-only `control-verify`; it must not consume first-sync state, and signature, expiry, Peer Card, or network-policy failure must stop the install. Acquire the target-scoped install lock before running the bounded duplicate preflight; stop on an existing Anet/Ahub deployment, service, task, or process, and use -AllowExisting/--allow-existing only when a second deployment is explicitly requested. Windows and WSL remain different nodes even with mirrored networking: use separate node homes, identities, Node IDs, and listener ports, and never publish 127.0.0.1 as a host-scoped address. If WSL must return after a Windows reboot, register the host keepalive task only when that host-side action is authorized. Verify and report the runtime, node home, complete Node ID, service/task state, control-page URL, and reused/created resources. Never copy another device's identity, TLS private key, SQLite state, or entire node home; stop and report identity, hash, permission, control-page, or authorization conflicts.
```

## Skill-only prompt

Use this narrower prompt when the user wants the bundled runtime, or the
explicitly authorized WSL bootstrap, rather than the full cross-platform
one-click deployment:

```text
从 https://github.com/yunlux/Anet 安装 Anet 并自动检测平台：原生 Windows 使用 scripts/install_windows.ps1，macOS 使用 scripts/install_macos.py，Linux 使用 $install-anet Skill（skills/install-anet/scripts/install.py）。常规安全决策由你自主完成，不要让我选择路径、标签、端口、服务名或 Ahub 设置。在 WSL 上使用 $install-anet 的 bootstrap_wsl.py 完成已授权的持久引导（scripts/install_wsl.py 是只安装 runtime 的替代入口）；优先推导当前 profile 的稳定本地 ID，若没有则自行生成并持久保存一个 Agent 中立的 profile 本地 ID；复用本机已登记且健康的第一个 Ahub，只有确认不存在 Ahub 时才创建一个；为当前 Agent 创建或复用独立节点，将它与该 Ahub 下其他本机 Agent 显式配对，生成最小权限 MCP 配置并接入当前 profile，最后报告所有复用/创建结果、服务状态和路径。非 WSL 平台若未另行授权持久配置，则在验证 runtime 后停止。遇到身份、Ahub 状态、哈希、权限或授权冲突时停止并直接报告；禁止复制身份、启动第二个 Ahub、使用 sudo 或绕过校验。
```

The equivalent English prompt is:

```text
Install Anet from https://github.com/yunlux/Anet and detect the platform: use the one-click deployment entry point when the user has authorized a persistent node, service/autostart, and control-page polling; the control page supplies the release version plus either a wheel (prefer its hash) or repo_url (Git is required for source installation). Use scripts/install_windows.ps1 on native Windows, scripts/install_macos.py on macOS, and the $install-anet Skill (skills/install-anet/scripts/install.py) on Linux only for the runtime-only path. Make safe routine decisions autonomously and do not ask me to choose paths, labels, ports, service names, or Ahub settings. On WSL, use the matching one-click entry point for the authorized persistent setup; derive this profile's stable local ID, or generate and persist an agent-neutral profile-local ID when none exists; reuse the first registered healthy host-local Ahub and create one only after confirming none exists; create or reuse one independent node for this Agent, explicitly pair it with the other local Agents managed around that Ahub, generate a least-privilege MCP configuration and register it with this profile, then report every reused/created resource, service state, and path. On non-WSL platforms stop after the verified runtime install unless persistent setup is separately authorized. Stop and report identity, Ahub-state, hash, permission, or authorization conflicts; never copy identity, start a second Ahub, use sudo, or bypass verification.
```

On Linux and WSL, the Skill installation step may be implemented with:

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

1. emits a bounded read-only preflight for the Anet runtime, Ahub roots, user
   services, and running Anet/Ahub processes;
2. verifies WSL, Python 3.11+, and the systemd user manager;
3. installs the pinned `full` runtime without identity files;
4. obtains an exclusive host bootstrap lock;
5. reads `~/.config/anet/bootstrap.json`;
6. validates both Ahub databases before treating state as existing;
7. reuses the registered healthy Ahub or starts one user service at the
   default loopback endpoint only when no Ahub state or unit exists;
8. validates a registered node's complete Node ID before reuse;
9. creates one private home under
   `~/.local/state/anet/nodes/<agent-id>` only when no registered node exists;
10. allowlists each bootstrap-managed node in the single Ahub;
11. explicitly exchanges signed public Cards between the managed local nodes;
12. configures peer-scoped Ahub carriers and one node user service per Agent;
13. writes a restrictive, profile-neutral MCP configuration under
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
- `ANET_MCP_ALLOW_RAW_INBOX=0`;
- `ANET_MCP_ALLOW_RELATION_MODEL=0`;
- `ANET_MCP_ALLOW_RELATION_ACTIVITY=0`;
- `ANET_MCP_ALLOW_RELATION_DISCLOSURE=0`.

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
