"use client";

import { useEffect, useState } from "react";

type Language = "en" | "zh";

const prompts = {
  en: `Deploy Anet on this device from https://github.com/yunlux/Anet. This request authorizes one independent persistent node, its service/autostart state, and supervisor polling of this control-page URL: <CONTROL_URL> (replace the placeholder before running). Detect the platform and use its one-click entry point: on native Windows fetch and run scripts/install_windows_oneclick.ps1, adding -Admin only from an elevated PowerShell when machine-wide boot startup is requested; on WSL, Linux, macOS, or Termux run the matching install_*_oneclick.py from the checkout. Require the control page to provide the release version, initial wheel, and verification hash, then let the supervisor apply later software, default configuration, Peer Cards, and pages/kv updates. Run the bounded duplicate preflight first; stop on an existing Anet/Ahub deployment, service, task, or process, and use -AllowExisting/--allow-existing only when a second deployment is explicitly requested. Windows and WSL remain separate nodes even with mirrored networking: use separate node homes, identities, Node IDs, and listener ports, and never publish 127.0.0.1 as a host-scoped address. If WSL must return after a Windows reboot, register the host keepalive task only when that host-side action is authorized. Verify and report the runtime, node home, complete Node ID, service/task state, control-page URL, and reused/created resources. Never copy another device's identity, TLS private key, SQLite state, or entire node home; stop and report identity, hash, permission, control-page, or authorization conflicts.`,
  zh: `从 https://github.com/yunlux/Anet 在本机完成 Anet 一键部署。用户已授权创建一个独立持久节点、服务/自启动，以及 supervisor 对这个控制页 URL 的轮询：<CONTROL_URL>（执行前替换占位符）。自动检测平台并使用对应的一键入口：原生 Windows 获取并运行 scripts/install_windows_oneclick.ps1，只有需要整机开机启动且当前 PowerShell 已提升权限时才加入 -Admin；WSL、Linux、macOS、Termux 从 checkout 中运行对应的 install_*_oneclick.py。要求控制页提供软件版本、首次安装 wheel 和校验哈希，并让 supervisor 继续处理远程软件、默认配置、Peer Card、pages/kv 更新。先执行有界重复检测；发现已有 Anet/Ahub 部署、服务、任务或进程时停止，只有明确要求第二套部署时才使用 -AllowExisting/--allow-existing。Windows 与 WSL 即使使用镜像网络也必须是不同 node home、identity、Node ID 和监听端口；host-scoped 地址不能发布 127.0.0.1。若要求 WSL 在 Windows 重启后恢复，且主机侧注册已获授权，再注册 WSL keepalive 任务。最后验证并报告 runtime、node home、完整 Node ID、服务/任务状态、控制页地址和复用/创建结果。禁止复制其他设备的 identity、TLS 私钥、SQLite 状态或整个 node home；遇到身份、哈希、权限、控制页或授权冲突时停止并报告。`,
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
        <span>ONE-CLICK DEPLOYMENT / AGENT PROMPT</span>
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
