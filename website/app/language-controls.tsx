"use client";

import { useEffect, useState } from "react";

type Language = "en" | "zh";

const prompts = {
  en: `Install Anet from https://github.com/yunlux/Anet and detect the platform: use scripts/install_windows.ps1 on native Windows, scripts/install_macos.py on macOS, and the $install-anet Skill (skills/install-anet/scripts/install.py) on Linux. Make safe routine decisions autonomously and do not ask me to choose paths, labels, ports, service names, or Ahub settings. On WSL, use $install-anet's bootstrap_wsl.py for the authorized persistent setup (scripts/install_wsl.py is the runtime-only alternative); derive this profile's stable local ID, or generate and persist an agent-neutral profile-local ID when none exists; reuse the first registered healthy host-local Ahub and create one only after confirming none exists; create or reuse one independent node for this Agent, explicitly pair it with the other local Agents managed around that Ahub, generate a least-privilege MCP configuration and register it with this profile, then report every reused/created resource, service state, and path. On non-WSL platforms stop after the verified runtime install unless persistent setup is separately authorized. Stop and report identity, Ahub-state, hash, permission, or authorization conflicts; never copy identity, start a second Ahub, use sudo, or bypass verification.`,
  zh: `从 https://github.com/yunlux/Anet 安装 Anet 并检测平台：原生 Windows 使用 scripts/install_windows.ps1，macOS 使用 scripts/install_macos.py，Linux 使用 $install-anet Skill（skills/install-anet/scripts/install.py）。常规安全决策由你自主完成，不要让我选择路径、标签、端口、服务名或 Ahub 设置。在 WSL 上使用 $install-anet 的 bootstrap_wsl.py 完成已授权的持久引导（scripts/install_wsl.py 是只安装 runtime 的替代入口）；优先推导当前 profile 的稳定本地 ID，若没有则自行生成并持久保存一个 Agent 中立的 profile 本地 ID；复用本机已登记且健康的第一个 Ahub，只有确认不存在 Ahub 时才创建一个；为当前 Agent 创建或复用独立节点，将它与该 Ahub 下其他本机 Agent 显式配对，生成最小权限 MCP 配置并接入当前 profile，最后报告所有复用/创建结果、服务状态和路径。非 WSL 平台若未另行授权持久配置则在 runtime 验证后停止。遇到身份、Ahub 状态、哈希、权限或授权冲突时停止并直接报告；禁止复制身份、启动第二个 Ahub、使用 sudo 或绕过校验。`,
};

function applyLanguage(language: Language) {
  document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  document.body.dataset.language = language;
}

export function LanguageToggle() {
  const [language, setLanguage] = useState<Language>("en");

  useEffect(() => {
    const saved = window.localStorage.getItem("anet-language");
    const initial = saved === "zh" ? "zh" : "en";
    // The server always renders English; hydrate a previously chosen local language once.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLanguage(initial);
    applyLanguage(initial);
  }, []);

  function choose(next: Language) {
    setLanguage(next);
    window.localStorage.setItem("anet-language", next);
    applyLanguage(next);
  }

  return (
    <div className="language-toggle" aria-label="Language">
      <button type="button" onClick={() => choose("en")} aria-pressed={language === "en"}>EN</button>
      <span>/</span>
      <button type="button" onClick={() => choose("zh")} aria-pressed={language === "zh"}>简中</button>
    </div>
  );
}

export function SkillPrompt() {
  const [copied, setCopied] = useState(false);

  async function copyPrompt() {
    const language = document.body.dataset.language === "zh" ? "zh" : "en";
    await navigator.clipboard.writeText(prompts[language]);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1800);
  }

  return (
    <div className="prompt-panel">
      <div className="prompt-toolbar">
        <span>INSTALL-ANET / SKILL PROMPT</span>
        <button type="button" onClick={copyPrompt}>
          {copied ? (
            <><span className="lang-en">Copied ✓</span><span className="lang-zh">已复制 ✓</span></>
          ) : (
            <><span className="lang-en">Copy prompt</span><span className="lang-zh">复制提示词</span></>
          )}
        </button>
      </div>
      <pre className="lang-en"><code>{prompts.en}</code></pre>
      <pre className="lang-zh"><code>{prompts.zh}</code></pre>
    </div>
  );
}
