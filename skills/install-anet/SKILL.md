---
name: install-anet
description: Install and verify the pinned Anet CLI plus stdio MCP runtime on Linux, or bootstrap one persistent WSL Agent around the single verified host-local Ahub when the user explicitly asks for automatic environment setup. Use for new Agent installation, WSL multi-Agent onboarding, reuse of an existing local Ahub, or exact runtime/configuration reporting. Runtime-only installation creates no identity or service; WSL bootstrap performs only the explicitly authorized local node, trust, Ahub, and user-service changes.
---

# Install Anet on Linux

Install the bundled, pinned release. Do not search for a newer package or use
an unverified wheel.

## Procedure

1. Confirm the host is Linux and `python3` is version 3.11 or newer.
2. Locate this Skill directory. Use the path returned by Hermes `skill_view`;
   do not assume the current working directory is the Skill directory.
3. Run:

   ```bash
   python3 <SKILL_DIR>/scripts/install.py
   ```

4. Parse the JSON result. Require:
   - `outcome` is `installed` or `reused`;
   - `version` is `0.12.1`;
   - `feature` is `mcp`;
   - `identity_files` is `0`;
   - `mcp_import` is `ok`.
5. Run the returned `cli` path with `--version`. Require `Anet 0.12.1`.
6. Report `runtime`, `cli`, `python`, and whether the install was new or
   reused.

## Authorized WSL host bootstrap

Use this only when the user explicitly asks to configure the persistent WSL
Agent, local Ahub, local peers, MCP environment, and user services—not for a
runtime-only install.

Do not ask the user to choose routine paths, labels, ports, service names,
Ahub settings, or recovery-free defaults. Select the deterministic safe
defaults below and continue. If the operation encounters an identity conflict,
incomplete managed state, failed integrity check, or authority outside the
request, stop once and report the bounded blocker instead of turning routine
installation into a questionnaire.

1. Determine the current runtime's stable, profile-scoped local identifier
   autonomously. Use `ANET_AGENT_ID` when already configured. Otherwise derive
   it from stable machine-readable profile metadata; if none exists, generate
   a random agent-neutral identifier and persist it in this profile's own local
   configuration before continuing. Do not use a human name, organizational
   role, IP address, or a label borrowed from another runtime.
2. Run:

   ```bash
   python3 <SKILL_DIR>/scripts/bootstrap_wsl.py \
     --agent-id <STABLE_LOCAL_AGENT_ID>
   ```

3. Parse the JSON result. Require:
   - `platform` is `wsl`;
   - `outcome` is `created` or `reused`;
   - `ahub.url`, `node.home`, `node.node_id`, and `mcp_config` are present;
   - the reported node and Ahub services are active.
4. Register the returned `mcp_config` with the current Agent through that
   runtime's native MCP configuration mechanism. Do not copy another
   profile's MCP file.
5. Report whether Ahub and the node were reused or created, the service names,
   node home, complete Node ID, MCP config path, and local Agent count.

The script holds a host bootstrap lock and is idempotent. It validates the
deployment registry and complete state before acting. It reuses the first
healthy registered Ahub; if unmanaged Ahub state or an unknown Ahub unit is
detected, it stops instead of starting a second service. With no Ahub, it
creates one user service and registers the current node as the first allowed
node. Later Agents receive distinct homes and identities, are explicitly
paired with bootstrap-managed local nodes, and use the same Ahub.

## Boundaries

- Install only under `~/.local/anet`; never install into the Hermes venv.
- Do not run `anet init` as part of installation.
- Do not create, copy, rename, repair, or infer an `ANET_HOME`.
- Do not edit a Hermes profile or MCP configuration without separate scope.
- Do not use `sudo`.
- Do not print secrets or inspect unrelated homes.
- Do not scan arbitrary home directories for nodes or Ahub state. Bootstrap
  may inspect only its registry, conventional state root, explicit arguments,
  and matching systemd user units.
- Never replace a missing registered node or incomplete Ahub state.
- If Python, networking, package dependencies, or hash verification fails,
  stop and report the exact bounded error. Do not weaken verification.

## After installation

If the user separately asks to bind or create a node, read
`references/after-install.md`. Otherwise stop after runtime verification.

CLI is the control plane for identity, trust, configuration, diagnostics, and
recovery. MCP is the data plane for a long-lived Agent's frequent messaging,
durable claims, and typed tasks.
