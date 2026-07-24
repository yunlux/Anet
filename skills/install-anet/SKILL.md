---
name: install-anet
description: Install and verify the pinned Anet CLI plus stdio MCP runtime on a new Linux host. Use when a Hermes Agent is asked to install Anet, prepare an Agent-ready Anet runtime, verify an existing Skill-managed Anet installation, or report the exact runtime paths. This Skill installs no node identity and makes no trust or service changes unless the user separately authorizes them.
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

## Boundaries

- Install only under `~/.local/anet`; never install into the Hermes venv.
- Do not run `anet init` as part of installation.
- Do not create, copy, rename, repair, or infer an `ANET_HOME`.
- Do not edit a Hermes profile or MCP configuration without separate scope.
- Do not use `sudo`.
- Do not print secrets or inspect unrelated homes.
- If Python, networking, package dependencies, or hash verification fails,
  stop and report the exact bounded error. Do not weaken verification.

## After installation

If the user separately asks to bind or create a node, read
`references/after-install.md`. Otherwise stop after runtime verification.

CLI is the control plane for identity, trust, configuration, diagnostics, and
recovery. MCP is the data plane for a long-lived Agent's frequent messaging,
durable claims, and typed tasks.
