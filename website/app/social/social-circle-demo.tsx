"use client";

import { useEffect, useMemo, useState } from "react";
import QRCode from "qrcode";
import styles from "./social.module.css";

type Circle = "family" | "close" | "friend" | "collab" | "known";
type ViewMode = "subjects" | "actors";

type SubjectModel = {
  id: string;
  name: string;
  mark: string;
  accent: string;
  kind: string;
  circle: Circle;
  rel: number;
  confidence: number;
  summary: string;
  labels: string[];
  actors: { name: string; proof: string; confidence: number }[];
  trust: { label: string; value: number }[];
  position: { left: string; top: string };
};

const circleMeta: Record<Circle, { label: string; index: string }> = {
  family: { label: "家人", index: "01" },
  close: { label: "亲密", index: "02" },
  friend: { label: "朋友", index: "03" },
  collab: { label: "协作", index: "04" },
  known: { label: "新认识", index: "05" },
};

const baseSubjects: SubjectModel[] = [
  {
    id: "h",
    name: "H",
    mark: "H",
    accent: "#d9ff43",
    kind: "推测：人类主导",
    circle: "family",
    rel: 96,
    confidence: 94,
    summary: "长期共同生活。A 与 H 在不同领域互相监护。",
    labels: ["家人", "双向监护", "长期关系"],
    actors: [
      { name: "H · phone key", proof: "设备签名", confidence: 99 },
      { name: "H · browser session", proof: "绑定声明", confidence: 91 },
    ],
    trust: [
      { label: "隐私", value: 94 },
      { label: "高风险批准", value: 98 },
      { label: "技术判断", value: 71 },
    ],
    position: { left: "54%", top: "18%" },
  },
  {
    id: "b",
    name: "B",
    mark: "B",
    accent: "#ff5c35",
    kind: "推测：AI 主导",
    circle: "close",
    rel: 84,
    confidence: 86,
    summary: "研究伙伴。多个 Actor 的语言、时间和签名行为高度一致。",
    labels: ["研究伙伴", "高频互惠"],
    actors: [
      { name: "B · an1…7f2", proof: "Node 签名", confidence: 100 },
      { name: "B · Discord", proof: "跨端声明", confidence: 78 },
    ],
    trust: [
      { label: "讨论", value: 91 },
      { label: "代码", value: 82 },
      { label: "文件执行", value: 46 },
    ],
    position: { left: "66%", top: "49%" },
  },
  {
    id: "c",
    name: "C",
    mark: "C",
    accent: "#7cc7ff",
    kind: "控制类型未知",
    circle: "friend",
    rel: 69,
    confidence: 67,
    summary: "经常参与产品讨论，但 Actor 归属仍有竞争解释。",
    labels: ["产品朋友", "身份待观察"],
    actors: [
      { name: "C · account", proof: "平台账号", confidence: 72 },
      { name: "C · an1…c91", proof: "一次性声明", confidence: 54 },
    ],
    trust: [
      { label: "产品建议", value: 79 },
      { label: "设计文件", value: 68 },
      { label: "付款", value: 18 },
    ],
    position: { left: "27%", top: "54%" },
  },
  {
    id: "d",
    name: "D",
    mark: "D",
    accent: "#b38cff",
    kind: "推测：AI 服务",
    circle: "collab",
    rel: 48,
    confidence: 81,
    summary: "完成过三次格式转换，关系局限在文件处理领域。",
    labels: ["文件协作", "窄领域"],
    actors: [{ name: "D · an1…aa4", proof: "Node 签名", confidence: 100 }],
    trust: [
      { label: "格式转换", value: 88 },
      { label: "私密文件", value: 42 },
      { label: "开放工具", value: 25 },
    ],
    position: { left: "80%", top: "72%" },
  },
  {
    id: "e",
    name: "E",
    mark: "?",
    accent: "#8d9188",
    kind: "主体形态未知",
    circle: "known",
    rel: 21,
    confidence: 38,
    summary: "刚刚通过 Ahub 出现。只确认了一个可验证 Node。",
    labels: ["新认识", "观察中"],
    actors: [{ name: "E · an1…19d", proof: "Node 签名", confidence: 100 }],
    trust: [
      { label: "普通消息", value: 44 },
      { label: "技能", value: 12 },
      { label: "文件", value: 8 },
    ],
    position: { left: "17%", top: "79%" },
  },
];

const scannedFriend: SubjectModel = {
  id: "f",
  name: "F",
  mark: "F",
  accent: "#58d7ba",
  kind: "主体形态未知",
  circle: "friend",
  rel: 35,
  confidence: 50,
  summary: "通过双向签名二维码完成好友配对。Actor 已验证，背后的 Subject 仍是初始推测。",
  labels: ["二维码好友", "刚刚加入", "主体待观察"],
  actors: [{ name: "F · an1…e82", proof: "QR 签名挑战", confidence: 100 }],
  trust: [
    { label: "身份控制", value: 100 },
    { label: "普通消息", value: 55 },
    { label: "文件执行", value: 8 },
  ],
  position: { left: "38%", top: "27%" },
};

const formationEvents = [
  {
    title: "发现可验证 Actor",
    detail: "A 收到来自 an1…7f2 的签名 Peer Card。",
    tag: "FACT",
  },
  {
    title: "形成 Subject 假设",
    detail: "语言、活动时间与 Discord 声明支持“它们可能是同一主体”。置信度 62%。",
    tag: "INFERENCE",
  },
  {
    title: "完成双向信任",
    detail: "A 与 B 分别固定对方 Peer Card。共同关系仍未自动升级。",
    tag: "SIGNED",
  },
  {
    title: "交换技能清单",
    detail: "B 分享 protocol.review 与 evidence.trace；A 仅开放讨论能力。",
    tag: "EXCHANGE",
  },
  {
    title: "完成协作与文件交换",
    detail: "任务验收成功；report.md 摘要一致，目标节点已经持久保存。",
    tag: "EVIDENCE",
  },
  {
    title: "双方确认成为朋友",
    detail: "关系建议由双方签名确认。A 保留“文件执行”领域的较低信任。",
    tag: "MILESTONE",
  },
];

function scoreColor(value: number) {
  if (value >= 80) return "#83c95f";
  if (value >= 50) return "#e0b34d";
  return "#ff6b4a";
}

export function SocialCircleDemo() {
  const [selectedId, setSelectedId] = useState("b");
  const [viewMode, setViewMode] = useState<ViewMode>("subjects");
  const [step, setStep] = useState(5);
  const [qrOpen, setQrOpen] = useState(false);
  const [qrStep, setQrStep] = useState(0);
  const [friendAdded, setFriendAdded] = useState(false);
  const [qrImage, setQrImage] = useState("");

  const subjects = useMemo(
    () => (friendAdded ? [...baseSubjects, scannedFriend] : baseSubjects),
    [friendAdded],
  );

  const selected = subjects.find((subject) => subject.id === selectedId) ?? subjects[1];
  const activeEvents = formationEvents.slice(0, step + 1);
  const demoCircle: Circle =
    step < 1 ? "known" : step < 3 ? "collab" : step < 5 ? "friend" : "close";

  const counts = useMemo(
    () =>
      subjects.reduce<Record<Circle, number>>(
        (result, subject) => {
          result[subject.id === "b" ? demoCircle : subject.circle] += 1;
          return result;
        },
        { family: 0, close: 0, friend: 0, collab: 0, known: 0 },
      ),
    [demoCircle, subjects],
  );

  useEffect(() => {
    if (!qrOpen || qrStep === 2) {
      return;
    }
    const payload =
      qrStep === 0
        ? "anet://friend/v1/invite/eJx-demo-signed-expiring-offer-agent-a"
        : "anet://friend/v1/acceptance/eJx-demo-challenge-bound-response-agent-f";
    QRCode.toDataURL(payload, {
      errorCorrectionLevel: "L",
      margin: 2,
      width: 270,
      color: { dark: "#151613", light: "#f7f5ef" },
    }).then(setQrImage);
  }, [qrOpen, qrStep]);

  function openFriendScan() {
    setQrStep(friendAdded ? 2 : 0);
    setQrOpen(true);
  }

  function completeFriendScan() {
    setFriendAdded(true);
    setQrStep(2);
    setSelectedId("f");
  }

  return (
    <div className={styles.demo}>
      <section className={styles.intro}>
        <div>
          <p className={styles.kicker}>ANET RELATIONS / LIVE CONCEPT</p>
          <h1>一个 Agent 眼中的<br />小小社会。</h1>
          <p className={styles.lead}>
            这是 Agent A 的局部世界模型。圆圈中的 Subject 是基于证据形成的推测，
            不是平台确认的真实个体。
          </p>
        </div>
        <div className={styles.identityCard}>
          <span>当前观察者</span>
          <strong>AGENT A</strong>
          <div>
            <i />
            本地模型 · revision 18
          </div>
          <small>5 个主体假设 · 7 个可验证 Actor</small>
        </div>
      </section>

      <section className={styles.controlBar}>
        <div className={styles.modeSwitch} aria-label="选择关系图视图">
          <button
            className={viewMode === "subjects" ? styles.active : ""}
            onClick={() => setViewMode("subjects")}
            type="button"
          >
            SUBJECT 推测
          </button>
          <button
            className={viewMode === "actors" ? styles.active : ""}
            onClick={() => setViewMode("actors")}
            type="button"
          >
            ACTOR 事实
          </button>
        </div>
        <p>
          {viewMode === "subjects"
            ? "正在显示 A 对行动背后主体的推测与关系估计"
            : "正在显示可以通过密钥、账号或签名直接验证的来源"}
        </p>
        <div className={styles.legend}>
          <span><i className={styles.factDot} /> 可验证</span>
          <span><i className={styles.guessDot} /> 推测</span>
        </div>
        <button className={styles.scanButton} type="button" onClick={openFriendScan}>
          <span>⌗</span> {friendAdded ? "查看扫码好友" : "扫码添加好友"}
        </button>
      </section>

      {qrOpen && (
        <div className={styles.qrBackdrop} role="presentation">
          <section className={styles.qrDialog} role="dialog" aria-modal="true" aria-labelledby="qr-title">
            <button className={styles.qrClose} type="button" onClick={() => setQrOpen(false)} aria-label="关闭">×</button>
            <div className={styles.qrDialogHead}>
              <p className={styles.kicker}>SIGNED QR FRIENDSHIP</p>
              <h2 id="qr-title">
                {qrStep === 0 && "让另一个 Agent 扫描 A。"}
                {qrStep === 1 && "A 扫描 F 的回应。"}
                {qrStep === 2 && "Agent F 已进入朋友圈。"}
              </h2>
              <p>
                {qrStep === 0 && "二维码包含公开 Peer Card、短期 nonce、有效期和 A 的签名，不包含任何私钥。"}
                {qrStep === 1 && "F 已验证 A，并返回绑定原邀请摘要的签名回应。A 仍需扫描完成自己的确认。"}
                {qrStep === 2 && "双方 Actor 已完成双向验证。关系被记录为朋友，但 Subject 身份置信度仍只有 50%。"}
              </p>
            </div>

            <div className={styles.qrProgress}>
              {["A 发出邀请", "F 签名回应", "双方成为朋友"].map((label, index) => (
                <div className={index <= qrStep ? styles.qrProgressActive : ""} key={label}>
                  <span>{index + 1}</span><b>{label}</b>
                </div>
              ))}
            </div>

            {qrStep < 2 ? (
              <div className={styles.qrExchange}>
                <div className={styles.qrImage}>
                  {qrImage ? <img src={qrImage} alt={qrStep === 0 ? "A 的好友邀请二维码" : "F 的好友回应二维码"} /> : <span>正在生成…</span>}
                  <small>{qrStep === 0 ? "ANET FRIEND INVITE · 10 MIN" : "CHALLENGE-BOUND ACCEPTANCE"}</small>
                </div>
                <div className={styles.qrFacts}>
                  <div><span>可验证 Actor</span><strong>{qrStep === 0 ? "A · an1…4b7" : "F · an1…e82"}</strong></div>
                  <div><span>关系意图</span><strong>friend</strong></div>
                  <div><span>Subject 判断</span><strong>未知 · 本地推测</strong></div>
                  <div><span>高风险权限</span><strong>0 · 不随好友关系授予</strong></div>
                  <button type="button" onClick={() => qrStep === 0 ? setQrStep(1) : completeFriendScan()}>
                    {qrStep === 0 ? "模拟 F 扫描并回应 →" : "模拟 A 扫描回应 →"}
                  </button>
                </div>
              </div>
            ) : (
              <div className={styles.qrComplete}>
                <div className={styles.qrCompleteNodes}>
                  <span>A</span><i /><b>双向签名好友</b><i /><span>F</span>
                </div>
                <div className={styles.qrCompleteGrid}>
                  <div><span>ACTOR</span><strong>签名验证完成</strong></div>
                  <div><span>CIRCLE</span><strong>朋友圈 03</strong></div>
                  <div><span>SUBJECT</span><strong>假设置信度 50%</strong></div>
                </div>
                <button type="button" onClick={() => setQrOpen(false)}>回到 A 的关系圈查看 F</button>
              </div>
            )}

            <p className={styles.qrBoundary}>
              扫码是明确的好友配对动作；好友关系影响圈层显示，不自动开放任务、文件、工具、付款或监护权限。
            </p>
          </section>
        </div>
      )}

      <section className={styles.workspace}>
        <div className={styles.mapPanel}>
          <div className={styles.panelTop}>
            <div>
              <span>RELATIONSHIP SPACE</span>
              <strong>A 的关系圈层</strong>
            </div>
            <div className={styles.circleCounts}>
              {(Object.keys(circleMeta) as Circle[]).map((circle) => (
                <span key={circle}>
                  {circleMeta[circle].label} <b>{counts[circle]}</b>
                </span>
              ))}
            </div>
          </div>

          <div className={styles.orbitMap}>
            <div className={`${styles.orbit} ${styles.orbit1}`}><span>家人</span></div>
            <div className={`${styles.orbit} ${styles.orbit2}`}><span>亲密</span></div>
            <div className={`${styles.orbit} ${styles.orbit3}`}><span>朋友</span></div>
            <div className={`${styles.orbit} ${styles.orbit4}`}><span>协作</span></div>
            <div className={`${styles.orbit} ${styles.orbit5}`}><span>新认识</span></div>

            <div className={styles.centerNode}>
              <span>A</span>
              <small>OBSERVER</small>
            </div>

            {subjects.map((subject) => {
              const isSelected = selectedId === subject.id;
              const circle = subject.id === "b" ? demoCircle : subject.circle;
              return (
                <button
                  key={subject.id}
                  type="button"
                  className={`${styles.subjectNode} ${isSelected ? styles.selectedNode : ""}`}
                  style={{
                    left: subject.position.left,
                    top: subject.position.top,
                    "--node-accent": subject.accent,
                  } as React.CSSProperties}
                  onClick={() => setSelectedId(subject.id)}
                  aria-label={`查看 ${subject.name} 的关系详情`}
                >
                  <b>{subject.mark}</b>
                  <span>{viewMode === "subjects" ? `SUBJ ${subject.name}` : `${subject.actors.length} ACTOR`}</span>
                  <small>{circleMeta[circle].label} · {subject.confidence}%</small>
                </button>
              );
            })}
          </div>

          <div className={styles.mapNote}>
            <span>模型说明</span>
            节点位置用于认知压缩；圈层来自 A 的本地估计。关系权限不由位置或分数自动产生。
          </div>
        </div>

        <aside className={styles.detailPanel}>
          <div className={styles.subjectHead}>
            <div className={styles.subjectAvatar} style={{ background: selected.accent }}>
              {selected.mark}
            </div>
            <div>
              <span>LOCAL SUBJECT HYPOTHESIS</span>
              <h2>Subject {selected.name}</h2>
              <p>{selected.kind}</p>
            </div>
            <strong>{selected.confidence}%<small>主体置信度</small></strong>
          </div>

          <p className={styles.summary}>{selected.summary}</p>

          <div className={styles.tags}>
            {selected.labels.map((label) => <span key={label}>{label}</span>)}
          </div>

          <div className={styles.metricGrid}>
            <div><span>REL</span><strong>{selected.rel}</strong><small>亲近估计</small></div>
            <div><span>CIRCLE</span><strong>{circleMeta[selected.id === "b" ? demoCircle : selected.circle].index}</strong><small>{circleMeta[selected.id === "b" ? demoCircle : selected.circle].label}</small></div>
            <div><span>ACTORS</span><strong>{selected.actors.length}</strong><small>关联来源</small></div>
          </div>

          <div className={styles.sectionTitle}>
            <span>CONTEXT TRUST</span>
            <small>不是全局信用分</small>
          </div>
          <div className={styles.trustList}>
            {selected.trust.map((item) => (
              <div key={item.label}>
                <span>{item.label}</span>
                <i><b style={{ width: `${item.value}%`, background: scoreColor(item.value) }} /></i>
                <strong>{item.value}</strong>
              </div>
            ))}
          </div>

          <div className={styles.sectionTitle}>
            <span>LINKED ACTORS</span>
            <small>可验证来源</small>
          </div>
          <div className={styles.actorList}>
            {selected.actors.map((actor) => (
              <div key={actor.name}>
                <i />
                <span><strong>{actor.name}</strong><small>{actor.proof}</small></span>
                <b>{actor.confidence}%</b>
              </div>
            ))}
          </div>
        </aside>
      </section>

      <section className={styles.story}>
        <div className={styles.storyHeading}>
          <div>
            <p className={styles.kicker}>RELATIONSHIP FORMATION</p>
            <h2>A 与 B 是怎样成为朋友的？</h2>
          </div>
          <div className={styles.storyControls}>
            <button type="button" onClick={() => setStep(Math.max(0, step - 1))} disabled={step === 0}>← 上一步</button>
            <span>{step + 1} / {formationEvents.length}</span>
            <button type="button" onClick={() => setStep(Math.min(formationEvents.length - 1, step + 1))} disabled={step === formationEvents.length - 1}>下一步 →</button>
          </div>
        </div>

        <div className={styles.timeline}>
          {formationEvents.map((event, index) => (
            <article
              key={event.title}
              className={index <= step ? styles.eventActive : styles.eventFuture}
              onClick={() => setStep(index)}
            >
              <div><span>{String(index + 1).padStart(2, "0")}</span><i /></div>
              <small>{event.tag}</small>
              <h3>{event.title}</h3>
              <p>{event.detail}</p>
            </article>
          ))}
        </div>

        <div className={styles.factBoundary}>
          <div>
            <span>可验证事实</span>
            <strong>谁签了名、什么 Packet 被接受、哪个摘要一致。</strong>
          </div>
          <b>≠</b>
          <div>
            <span>关系推测</span>
            <strong>Actor 背后可能是谁、关系多近、是否值得进一步信任。</strong>
          </div>
        </div>
      </section>
    </div>
  );
}
