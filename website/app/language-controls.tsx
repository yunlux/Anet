"use client";

import { useEffect, useState } from "react";

type Language = "en" | "zh";

const prompts = {
  en: `Install Anet on this Linux machine using the bundled skills/install-anet Skill from the official Anet repository. If you cannot access that repository, stop and ask me for its URL.

Install and verify the pinned CLI + MCP runtime only. Do not create or copy a node identity, run anet init, change trust, edit an agent profile, register a service, use sudo, or bypass any safety check. Report whether the runtime was installed or reused, the Anet version, the runtime/python/CLI paths, MCP import result, and identity file count.`,
  zh: `请使用 Anet 官方仓库内置的 skills/install-anet Skill，在这台 Linux 电脑上安装 Anet。如果你无法访问该仓库，请停止并向我询问仓库地址。

本次只安装并验证固定版本的 CLI + MCP runtime。不要创建或复制节点身份，不要运行 anet init，不要修改信任、Agent profile 或注册服务，不要使用 sudo，也不要绕过任何安全检查。最后报告 installed/reused、Anet 版本、runtime/python/CLI 路径、MCP 导入结果和 identity file 数量。`,
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
