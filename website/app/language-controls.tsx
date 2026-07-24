"use client";

import { useEffect, useState } from "react";

type Language = "en" | "zh";

const prompts = {
  en: `Install and verify Anet from the official repository on this machine. Detect the platform and use its bundled pinned installer: native Windows → scripts/install_windows.ps1; WSL → scripts/install_wsl.py; other Linux → skills/install-anet (or its scripts/install.py). Install the mcp feature only. If the repository is unavailable, stop and ask me for its URL.

Do not create or copy a node identity, run anet init, change trust, edit an agent profile, register a service, use sudo, or bypass hash checks. Report platform, installed/reused, version, runtime/Python/CLI paths, MCP import result, and identity file count.`,
  zh: `请从 Anet 官方仓库在这台电脑上安装并验证 Anet。先检测平台，再使用仓库内置的固定版本安装入口：原生 Windows → scripts/install_windows.ps1；WSL → scripts/install_wsl.py；其他 Linux → skills/install-anet（或其中的 scripts/install.py）。只安装 mcp feature。如果无法访问仓库，请停止并向我询问仓库地址。

不要创建或复制节点身份，不要运行 anet init，不要修改信任、Agent profile 或注册服务，不要使用 sudo，也不要绕过哈希检查。最后报告平台、installed/reused、版本、runtime/Python/CLI 路径、MCP 导入结果和 identity file 数量。`,
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
