# Install Anet on a new Linux Hermes Agent with one Skill prompt

The repository contains a self-contained Hermes-compatible Skill at
`skills/install-anet`. It bundles the pinned Anet wheel, verifies its SHA-256,
installs an isolated `mcp` runtime under `~/.local/anet`, imports the MCP
server, verifies the CLI, and confirms that no node identity was created.

## Distribution prerequisite

The new computer must be able to read this repository from a public GitHub
repository or an authenticated private GitHub repository. Replace
`<OWNER>/<REPOSITORY>` below with the actual published repository.

This repository currently has no Git remote. Until it is published at a stable
address, no cross-computer prompt can truthfully guarantee retrieval.

## One prompt to send the new Hermes Agent

```text
请在这台 Linux 电脑上安装 Anet。先使用 Hermes Skills 安装
<OWNER>/<REPOSITORY>/skills/install-anet，并让新 Skill 立即生效；
然后调用 /install-anet 完成固定版本的 CLI+MCP runtime 安装与验证。
本次只授权安装和验证 runtime：不要创建或复制节点身份，不要运行 anet init，
不要修改 Hermes profile、信任关系或 systemd 服务。最后报告 installed/reused、
Anet 版本、runtime/python/cli 绝对路径、MCP 导入结果和 identity_files 数量。
任何哈希、Python 版本、依赖下载或 Skill 安全检查失败时停止，不要使用 --force
绕过，也不要使用 sudo。
```

Hermes may implement the first sentence with:

```bash
hermes skills install <OWNER>/<REPOSITORY>/skills/install-anet --now
```

Official Hermes behavior is that the directory, bundled script, references,
and wheel asset are copied together into `~/.hermes/skills/`. `--now`
invalidates the Skill prompt cache so `/install-anet` can be used immediately.

## Expected result

The Skill must return JSON equivalent to:

```json
{
  "outcome": "installed",
  "version": "0.12.1",
  "feature": "mcp",
  "runtime": "/home/<user>/.local/anet/versions/0.12.1-mcp/venv",
  "python": "/home/<user>/.local/anet/current/venv/bin/python",
  "cli": "/home/<user>/.local/anet/current/venv/bin/anet",
  "identity_files": 0,
  "mcp_import": "ok"
}
```

`outcome="reused"` is valid on a repeated run. The CLI must additionally
return `Anet 0.12.1`.

## Preconditions

- Linux and Python 3.11 or newer;
- a working Hermes installation with terminal and Skills support;
- HTTPS access to the GitHub repository and Python dependency index;
- `GITHUB_TOKEN` when the repository is private or unauthenticated GitHub rate
  limits are exhausted.

The Skill never installs into the Hermes venv and never uses `sudo`.

## After runtime installation

Creating or binding a persistent node is intentionally a separate prompt. If
the user explicitly authorizes it, invoke `/install-anet` with that additional
request; the Skill will load `references/after-install.md`. A missing expected
home is not permission to create a replacement identity.

## Official Hermes references

- Skills workflow:
  https://github.com/NousResearch/hermes-agent/blob/main/website/docs/guides/work-with-skills.md
- Skills system and direct GitHub installs:
  https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/skills.md
