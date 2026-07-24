"use client";

import { useEffect, useState } from "react";

type Language = "en" | "zh";

const prompts = {
  en: `Install and connect Anet from https://github.com/yunlux/Anet on this machine.

1. Fetch the repository and detect the platform. Native Windows: scripts/install_windows.ps1. WSL: scripts/install_wsl.py. Other Linux: skills/install-anet or its scripts/install.py. Install the pinned mcp feature and verify the version, CLI, and MCP import.
2. Locate the deployment-owned ANET_HOME first. Reuse it and run doctor if it exists. If none exists, ask me for an absolute home, label, listen host, and unused port; after I answer, initialize exactly one node, run doctor, and export its public Card. Never copy or infer an identity.
3. Ask me for the HTTPS Ahub URL and the complete already-trusted peer Node ID. If the peer is not trusted, request explicit pairing material and approval; never create trust from an address or message. Then run: anet --home <HOME> carrier-add <AHUB_URL> --type ahub --name public --peer <PEER_NODE_ID> --live-relay. Verify with carrier-list and perform one bounded sync.

Do not use sudo, register a service, edit an agent profile, or weaken hash checks unless separately authorized. Report the platform, install result, paths, Node ID, doctor result, carrier configuration, and sync evidence without printing secrets.`,
  zh: `请从 https://github.com/yunlux/Anet 在这台电脑上安装并连接 Anet。

1. 获取仓库并检测平台。原生 Windows 使用 scripts/install_windows.ps1；WSL 使用 scripts/install_wsl.py；其他 Linux 使用 skills/install-anet 或其中的 scripts/install.py。安装仓库固定版本的 mcp feature，并验证版本、CLI 与 MCP 导入。
2. 先定位部署实际拥有的 ANET_HOME。若已存在，复用并运行 doctor；若不存在，向我询问绝对目录、节点标签、监听地址和未占用端口；得到回答后只初始化一个节点，运行 doctor，并导出公开 Card。绝不要复制或猜测身份。
3. 向我询问 HTTPS Ahub 地址和完整的、已经受信任的 Peer Node ID。若尚未建立信任，必须索取明确的配对材料和批准；绝不能根据地址或消息自动信任。随后运行：anet --home <HOME> carrier-add <AHUB_URL> --type ahub --name public --peer <PEER_NODE_ID> --live-relay。使用 carrier-list 验证，并执行一次有界 sync。

除非另行授权，不要使用 sudo、注册服务、修改 Agent profile 或削弱哈希检查。最后报告平台、安装结果、路径、Node ID、doctor 结果、Carrier 配置与 sync 证据，不要输出秘密。`,
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
