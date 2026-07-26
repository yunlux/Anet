"use client";

import { useEffect, useMemo, useState } from "react";
import QRCode from "qrcode";
import styles from "./social.module.css";

type Circle = "family" | "close" | "friend" | "collab" | "known";
type ViewMode = "subjects" | "actors";

type RelationSnapshot = {
  version: number;
  observer_actor_id: string;
  actors: {
    actor_id: string;
    actor_kind?: string;
    actor_label: string;
    state: string;
    proofs?: {
      proof_type: string;
      scope: string;
      issuer_actor_id: string;
    }[];
  }[];
  subjects: {
    subject_ref: string;
    state: string;
    labels: string[];
    confidence: number;
    actor_links: {
      actor_id: string;
      confidence: number;
    }[];
  }[];
  relationships: {
    subject_ref: string;
    circle: string;
    state: string;
    relationship_labels: string[];
    relationship_confidence: number;
    context_trust: {
      context: string;
      estimate: number;
      confidence: number;
    }[];
  }[];
  interaction_stats?: {
    subject_ref: string;
    context: string;
    facet: string;
    incoming: number;
    outgoing: number;
    outcomes: Record<string, number>;
  }[];
  subject_transitions?: {
    transition_id: string;
    transition_type: "split" | "merge" | "supersede";
    source_subject_refs: string[];
    replacement_subject_refs: string[];
    confidence: number;
  }[];
  relationship_suggestions?: {
    suggestion_id: string;
    suggestion_type: "circle.advance" | "context-trust.review";
    subject_ref: string;
    confidence: number;
    proposed_circle: string;
    context: string;
    proposed_estimate: number | null;
    metrics: Record<string, number>;
    requires_explicit_action: boolean;
    authorization_effect: string;
  }[];
  suggestion_decisions?: {
    decision_id: string;
    suggestion_id: string;
    suggestion_type: "circle.advance" | "context-trust.review";
    subject_ref: string;
    decision: "accepted" | "rejected";
    rationale: string;
    proposed_circle: string;
    context: string;
    proposed_estimate: number | null;
    applied: boolean;
    authorization_effect: string;
  }[];
  relationship_activity?: {
    activities: {
      activity_id: string;
      activity_type: string;
      category: "actor" | "subject" | "interaction" | "relationship" | "decision";
      fact_level: "verified" | "inference" | "estimate" | "decision";
      actor_id: string;
      subject_ref: string;
      occurred_ms: number;
      details: Record<string, unknown>;
      privacy: string;
      authorization_effect: string;
    }[];
    next_cursor: string;
    has_more: boolean;
    ordering: string;
    privacy: string;
  };
};

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
  activity?: { label: string; count: number; detail: string }[];
  lineage?: { label: string; detail: string; confidence: number }[];
  suggestions?: {
    id: string;
    title: string;
    detail: string;
    confidence: number;
  }[];
  decisions?: {
    id: string;
    decision: "accepted" | "rejected";
    title: string;
    rationale: string;
  }[];
  position: { left: string; top: string };
};

type TimelineEvent = {
  id: string;
  title: string;
  detail: string;
  tag: string;
};

const circleMeta: Record<Circle, { label: string; index: string }> = {
  family: { label: "家人", index: "01" },
  close: { label: "亲密", index: "02" },
  friend: { label: "朋友", index: "03" },
  collab: { label: "协作", index: "04" },
  known: { label: "新认识", index: "05" },
};

const importPositions = [
  { left: "54%", top: "18%" },
  { left: "66%", top: "49%" },
  { left: "27%", top: "54%" },
  { left: "80%", top: "72%" },
  { left: "17%", top: "79%" },
  { left: "38%", top: "27%" },
  { left: "73%", top: "27%" },
  { left: "32%", top: "73%" },
];
const importAccents = [
  "#d9ff43",
  "#ff5c35",
  "#7cc7ff",
  "#b38cff",
  "#58d7ba",
  "#f1c85b",
  "#f28fbc",
  "#8d9188",
];

function isCircle(value: string): value is Circle {
  return value in circleMeta;
}

function projectSnapshot(value: unknown): {
  observer: string;
  subjects: SubjectModel[];
  activities: TimelineEvent[];
} {
  if (!value || typeof value !== "object") {
    throw new Error("模型必须是 JSON 对象");
  }
  const snapshot = value as RelationSnapshot;
  if (
    ![2, 3, 4, 5, 6, 7].includes(snapshot.version) ||
    !Array.isArray(snapshot.actors) ||
    !Array.isArray(snapshot.subjects) ||
    !Array.isArray(snapshot.relationships)
  ) {
    throw new Error("仅支持 relation-list --model 输出的 v2-v7 模型");
  }
  const actors = new Map(snapshot.actors.map((actor) => [actor.actor_id, actor]));
  const relationships = new Map(
    snapshot.relationships.map((relationship) => [
      relationship.subject_ref,
      relationship,
    ]),
  );
  const interactionStats = Array.isArray(snapshot.interaction_stats)
    ? snapshot.interaction_stats
    : [];
  const transitions = Array.isArray(snapshot.subject_transitions)
    ? snapshot.subject_transitions
    : [];
  const suggestions = Array.isArray(snapshot.relationship_suggestions)
    ? snapshot.relationship_suggestions
    : [];
  const decisions = Array.isArray(snapshot.suggestion_decisions)
    ? snapshot.suggestion_decisions
    : [];
  const projected = snapshot.subjects
    .filter((subject) => subject.state === "active")
    .map((subject, index) => {
    const relationship = relationships.get(subject.subject_ref);
    const circle =
      relationship && isCircle(relationship.circle)
        ? relationship.circle
        : "known";
    const links = Array.isArray(subject.actor_links) ? subject.actor_links : [];
    const labels = [
      ...(Array.isArray(subject.labels) ? subject.labels : []),
      ...(Array.isArray(relationship?.relationship_labels)
        ? relationship.relationship_labels
        : []),
    ];
    return {
      id: subject.subject_ref,
      name: String(index + 1).padStart(2, "0"),
      mark: String(index + 1),
      accent: importAccents[index % importAccents.length],
      kind:
        subject.state === "superseded"
          ? "已被新证据取代的假设"
          : "本地 Subject 假设",
      circle,
      rel: Number(relationship?.relationship_confidence ?? 0),
      confidence: Number(subject.confidence ?? 0),
      summary: `${links.length} 个 Actor 链接到这个推测主体。该归并只在当前观察者的本地模型中成立。`,
      labels: labels.length ? [...new Set(labels)] : ["主体待观察"],
      actors: links.map((link) => {
        const actor = actors.get(link.actor_id);
        return {
          name: actor?.actor_label
            ? `${actor.actor_label} · ${link.actor_id.slice(0, 11)}…`
            : `${link.actor_id.slice(0, 14)}…`,
          proof:
            actor?.state === "revoked"
              ? "Actor 已撤销"
              : Array.isArray(actor?.proofs)
                ? proofLabel(actor.proofs[0]?.scope)
                : "Node 签名",
          confidence: Number(link.confidence ?? 0),
        };
      }),
      trust: Array.isArray(relationship?.context_trust)
        ? relationship.context_trust.map((item) => ({
            label: item.context,
            value: Number(item.estimate ?? 0),
          }))
        : [],
      activity: interactionStats
        .filter((item) => item.subject_ref === subject.subject_ref)
        .map((item) => ({
          label: `${item.facet} · ${item.context}`,
          count: Number(item.incoming ?? 0) + Number(item.outgoing ?? 0),
          detail: `收 ${Number(item.incoming ?? 0)} · 发 ${Number(item.outgoing ?? 0)}`,
        })),
      lineage: transitions
        .filter((item) =>
          item.replacement_subject_refs.includes(subject.subject_ref),
        )
        .map((item) => ({
          label:
            item.transition_type === "merge"
              ? "由多个假设合并产生"
              : item.transition_type === "split"
                ? "由一个假设拆分产生"
                : "由旧假设修订产生",
          detail: item.source_subject_refs
            .map((ref) => `${ref.slice(0, 13)}…`)
            .join(" + "),
          confidence: Number(item.confidence ?? 0),
        })),
      suggestions: suggestions
        .filter((item) => item.subject_ref === subject.subject_ref)
        .map((item) => {
          if (item.suggestion_type === "circle.advance") {
            const circle = isCircle(item.proposed_circle)
              ? circleMeta[item.proposed_circle].label
              : item.proposed_circle;
            return {
              id: item.suggestion_id,
              title: `建议进入${circle}圈`,
              detail: `${Number(item.metrics.balanced_task_events ?? 0)} 组平衡任务事件 · 需明确采纳`,
              confidence: Number(item.confidence ?? 0),
            };
          }
          return {
            id: item.suggestion_id,
            title: `复核 ${item.context} 信任`,
            detail: `候选 ${Number(item.proposed_estimate ?? 0)} · 样本 ${Number(item.metrics.sample_size ?? 0)} · 不自动应用`,
            confidence: Number(item.confidence ?? 0),
          };
        }),
      decisions: decisions
        .filter((item) => item.subject_ref === subject.subject_ref)
        .map((item) => ({
          id: item.decision_id,
          decision: item.decision,
          title:
            item.suggestion_type === "circle.advance"
              ? `${item.decision === "accepted" ? "已采纳" : "已拒绝"}进入${
                  isCircle(item.proposed_circle)
                    ? circleMeta[item.proposed_circle].label
                    : item.proposed_circle
                }圈`
              : `${item.decision === "accepted" ? "已采纳" : "已拒绝"} ${
                  item.context
                } 信任复核`,
          rationale: item.rationale,
        })),
      position: importPositions[index % importPositions.length],
    } satisfies SubjectModel;
    });
  if (!projected.length) {
    throw new Error("模型中还没有 Subject 假设");
  }
  return {
    observer: String(snapshot.observer_actor_id || "local observer"),
    subjects: projected,
    activities: Array.isArray(snapshot.relationship_activity?.activities)
      ? snapshot.relationship_activity.activities
          .slice(-12)
          .map(projectActivity)
      : [],
  };
}

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
    labels: ["研究伙伴", "双方签名关系", "高频互惠"],
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
    circle: "known",
    rel: 48,
    confidence: 81,
    summary: "完成过三次格式转换。系统建议进入协作圈，但 A 尚未明确采纳。",
    labels: ["文件协作候选", "窄领域"],
    actors: [{ name: "D · an1…aa4", proof: "Node 签名", confidence: 100 }],
    trust: [
      { label: "格式转换", value: 88 },
      { label: "私密文件", value: 42 },
      { label: "开放工具", value: 25 },
    ],
    suggestions: [
      {
        id: "rsg_demo_d_collab",
        title: "建议进入协作圈",
        detail: "重复任务提交与完成 · 双向事件 · 不涉及授权",
        confidence: 58,
      },
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

const formationEvents: TimelineEvent[] = [
  {
    id: "formation-actor",
    title: "发现可验证 Actor",
    detail: "A 收到来自 an1…7f2 的签名 Peer Card。",
    tag: "FACT",
  },
  {
    id: "formation-subject",
    title: "形成 Subject 假设",
    detail: "语言、活动时间与 Discord 声明支持“它们可能是同一主体”。置信度 62%。",
    tag: "INFERENCE",
  },
  {
    id: "formation-trust",
    title: "完成双向信任",
    detail: "A 与 B 分别固定对方 Peer Card。共同关系仍未自动升级。",
    tag: "SIGNED",
  },
  {
    id: "formation-skill",
    title: "交换技能清单",
    detail: "B 分享 protocol.review 与 evidence.trace；A 仅开放讨论能力。",
    tag: "EXCHANGE",
  },
  {
    id: "formation-task",
    title: "完成协作与文件交换",
    detail: "任务验收成功；report.md 摘要一致，目标节点已经持久保存。",
    tag: "EVIDENCE",
  },
  {
    id: "formation-friend",
    title: "双方确认成为朋友",
    detail: "A 与 B 签署同一份 Actor-to-Actor relationship claim；A 仍保留“文件执行”领域的较低信任。",
    tag: "MILESTONE",
  },
];

function projectActivity(
  item: NonNullable<
    RelationSnapshot["relationship_activity"]
  >["activities"][number],
): TimelineEvent {
  const details = item.details ?? {};
  const subject = item.subject_ref
    ? `${item.subject_ref.slice(0, 13)}…`
    : "—";
  const tags = {
    verified: "FACT",
    inference: "INFERENCE",
    estimate: "ESTIMATE",
    decision: "DECISION",
  };
  if (item.activity_type === "actor.observed") {
    return {
      id: item.activity_id,
      title: "观察到可验证 Actor",
      detail: `${String(details.actor_kind || "actor")} · ${String(
        item.actor_id,
      ).slice(0, 14)}… · 归入 ${subject}`,
      tag: tags[item.fact_level],
    };
  }
  if (item.activity_type === "interaction.observed") {
    const facets = Array.isArray(details.facets)
      ? details.facets.join(" + ")
      : "interaction";
    return {
      id: item.activity_id,
      title: `${String(details.direction || "local")} ${facets}`,
      detail: `${String(details.context || "context")} · ${String(
        details.outcome || "observed",
      )} · ${subject}`,
      tag: tags[item.fact_level],
    };
  }
  if (item.activity_type === "relationship.circle-set") {
    return {
      id: item.activity_id,
      title: `圈层设为 ${String(details.circle || "unknown")}`,
      detail: `关系置信度 ${Number(details.confidence ?? 0)}% · ${subject} · 不改变授权`,
      tag: tags[item.fact_level],
    };
  }
  if (item.activity_type === "relationship.context-trust-set") {
    return {
      id: item.activity_id,
      title: `复核 ${String(details.context || "context")}`,
      detail: `估计 ${Number(details.estimate ?? 0)} · 置信度 ${Number(
        details.confidence ?? 0,
      )}% · 仅限该上下文`,
      tag: tags[item.fact_level],
    };
  }
  if (item.activity_type.startsWith("relationship.suggestion-")) {
    return {
      id: item.activity_id,
      title:
        String(details.decision) === "accepted"
          ? "采纳关系建议"
          : "拒绝关系建议",
      detail: `${String(details.suggestion_type || "suggestion")} · ${
        details.applied ? "已应用提议" : "关系未改变"
      } · 权限影响为零`,
      tag: tags[item.fact_level],
    };
  }
  if (item.activity_type.startsWith("subject.")) {
    return {
      id: item.activity_id,
      title:
        item.activity_type === "subject.actor-linked"
          ? "修订 Actor–Subject 链接"
          : `Subject ${String(details.transition_type || "revision")}`,
      detail: `${subject} · 推测修订，不是身份事实`,
      tag: tags[item.fact_level],
    };
  }
  return {
    id: item.activity_id,
    title: item.activity_type,
    detail: `${subject} · observer-local activity`,
    tag: tags[item.fact_level],
  };
}

function scoreColor(value: number) {
  if (value >= 80) return "#83c95f";
  if (value >= 50) return "#e0b34d";
  return "#ff6b4a";
}

function proofLabel(scope: string | undefined) {
  if (scope === "cryptographic") return "密码学签名";
  if (scope === "platform-observed") return "平台 Adapter 观察";
  if (scope === "bridge-attested") return "桥接 Node 证明";
  if (scope === "operator-attested") return "本地操作方声明";
  return "来源已记录";
}

export function SocialCircleDemo() {
  const [selectedId, setSelectedId] = useState("d");
  const [viewMode, setViewMode] = useState<ViewMode>("subjects");
  const [step, setStep] = useState(5);
  const [qrOpen, setQrOpen] = useState(false);
  const [qrStep, setQrStep] = useState(0);
  const [friendAdded, setFriendAdded] = useState(false);
  const [qrImage, setQrImage] = useState("");
  const [importedSubjects, setImportedSubjects] = useState<SubjectModel[] | null>(null);
  const [importedObserver, setImportedObserver] = useState("");
  const [importedActivity, setImportedActivity] = useState<TimelineEvent[]>([]);
  const [importError, setImportError] = useState("");
  const [disclosureSent, setDisclosureSent] = useState(false);
  const [disclosureSchedule, setDisclosureSchedule] = useState<
    "off" | "active" | "revoked"
  >("off");
  const [demoDecisions, setDemoDecisions] = useState<
    Record<string, "accepted" | "rejected">
  >({});

  const subjects = useMemo(() => {
    if (importedSubjects) {
      return importedSubjects;
    }
    const source = friendAdded ? [...baseSubjects, scannedFriend] : baseSubjects;
    return source.map((subject) => {
      const decisions = (subject.suggestions ?? [])
        .filter((item) => demoDecisions[item.id])
        .map((item) => ({
          id: `rsd_${item.id}`,
          decision: demoDecisions[item.id],
          title: `${
            demoDecisions[item.id] === "accepted" ? "已采纳" : "已拒绝"
          }${item.title.replace("建议", "")}`,
          rationale:
            demoDecisions[item.id] === "accepted"
              ? "demo:bounded-collaboration-confirmed"
              : "demo:insufficient-social-context",
        }));
      const accepted = decisions.some((item) => item.decision === "accepted");
      return {
        ...subject,
        circle: accepted && subject.id === "d" ? "collab" : subject.circle,
        rel: accepted && subject.id === "d" ? 58 : subject.rel,
        suggestions: (subject.suggestions ?? []).filter(
          (item) => !demoDecisions[item.id],
        ),
        decisions,
      } satisfies SubjectModel;
    });
  }, [demoDecisions, friendAdded, importedSubjects]);

  const selected =
    subjects.find((subject) => subject.id === selectedId) ??
    subjects[1] ??
    subjects[0];
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

  async function importModel(file: File | undefined) {
    if (!file) {
      return;
    }
    try {
      const projected = projectSnapshot(JSON.parse(await file.text()));
      setImportedSubjects(projected.subjects);
      setImportedObserver(projected.observer);
      setImportedActivity(projected.activities);
      setSelectedId(projected.subjects[0].id);
      setStep(Math.max(0, projected.activities.length - 1));
      setFriendAdded(false);
      setImportError("");
    } catch (error) {
      setImportError(error instanceof Error ? error.message : "无法读取关系模型");
    }
  }

  function resetModel() {
    setImportedSubjects(null);
    setImportedObserver("");
    setImportedActivity([]);
    setSelectedId("d");
    setStep(formationEvents.length - 1);
    setImportError("");
    setDemoDecisions({});
    setDisclosureSent(false);
  }

  function decideDemoSuggestion(
    suggestionId: string,
    decision: "accepted" | "rejected",
  ) {
    setDemoDecisions((current) => ({
      ...current,
      [suggestionId]: decision,
    }));
    setStep(formationEvents.length + (decision === "accepted" ? 1 : 0));
  }

  const timelineEvents = useMemo(() => {
    if (importedSubjects) {
      return importedActivity.length
        ? importedActivity
        : [
            {
              id: "legacy-model-no-activity",
              title: "模型未包含活动投影",
              detail:
                "请使用当前版本的 relation-list --model 重新导出，即可回放本地追加历史。",
              tag: "NOTICE",
            },
          ];
    }
    const decision = demoDecisions.rsg_demo_d_collab;
    if (!decision) {
      return formationEvents;
    }
    const changes: TimelineEvent[] =
      decision === "accepted"
        ? [
            {
              id: "demo-circle-applied",
              title: "D 进入协作圈",
              detail: "关系建议被显式采纳；只改变本地圈层，权限影响为零。",
              tag: "ESTIMATE",
            },
          ]
        : [];
    return [
      ...formationEvents,
      ...changes,
      {
        id: `demo-decision-${decision}`,
        title: decision === "accepted" ? "采纳 D 的建议" : "拒绝 D 的建议",
        detail:
          decision === "accepted"
            ? "决定与圈层变更写入同一次本地保存，可回放、不可反转。"
            : "拒绝决定已记录，D 仍留在新认识圈，未发生关系变更。",
        tag: "DECISION",
      },
    ];
  }, [demoDecisions, importedActivity, importedSubjects]);

  const actorCount = new Set(
    subjects.flatMap((subject) => subject.actors.map((actor) => actor.name)),
  ).size;

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
          <strong>{importedSubjects ? "LOCAL MODEL" : "AGENT A"}</strong>
          <div>
            <i />
            {importedSubjects
              ? `${importedObserver.slice(0, 18)}…`
              : "本地模型 · revision 18"}
          </div>
          <small>{subjects.length} 个主体假设 · {actorCount} 个 Actor 链接</small>
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
        <label className={styles.importButton}>
          <span>↥</span> 导入本地模型
          <input
            type="file"
            accept="application/json,.json"
            onChange={(event) => void importModel(event.target.files?.[0])}
          />
        </label>
        {importedSubjects && (
          <button className={styles.resetButton} type="button" onClick={resetModel}>
            返回示例
          </button>
        )}
        <button className={styles.scanButton} type="button" onClick={openFriendScan}>
          <span>⌗</span> {friendAdded ? "查看扫码好友" : "扫码添加好友"}
        </button>
      </section>

      {importError && <p className={styles.importError}>{importError}</p>}

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

          {selected.suggestions && selected.suggestions.length > 0 && (
            <>
              <div className={styles.sectionTitle}>
                <span>RELATIONSHIP SUGGESTIONS</span>
                <small>只读候选 · 需明确采纳</small>
              </div>
              <div className={styles.suggestionList}>
                {selected.suggestions.map((item) => (
                  <article key={`${item.title}:${item.detail}`}>
                    <div>
                      <strong>{item.title}</strong>
                      <small>{item.detail}</small>
                    </div>
                    <b>{item.confidence}%</b>
                    {!importedSubjects && (
                      <div className={styles.decisionActions}>
                        <button
                          type="button"
                          onClick={() =>
                            decideDemoSuggestion(item.id, "accepted")
                          }
                        >
                          采纳
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            decideDemoSuggestion(item.id, "rejected")
                          }
                        >
                          拒绝
                        </button>
                      </div>
                    )}
                  </article>
                ))}
              </div>
            </>
          )}

          {selected.decisions && selected.decisions.length > 0 && (
            <>
              <div className={styles.sectionTitle}>
                <span>SUGGESTION DECISIONS</span>
                <small>不可变历史 · 权限影响为零</small>
              </div>
              <div className={styles.decisionList}>
                {selected.decisions.map((item) => (
                  <article key={item.id}>
                    <b data-decision={item.decision}>
                      {item.decision === "accepted" ? "ACCEPTED" : "REJECTED"}
                    </b>
                    <div>
                      <strong>{item.title}</strong>
                      <small>{item.rationale}</small>
                    </div>
                  </article>
                ))}
              </div>
            </>
          )}

          {selected.activity && selected.activity.length > 0 && (
            <>
              <div className={styles.sectionTitle}>
                <span>INTERACTION EVIDENCE</span>
                <small>仅元数据 · 不等于信任</small>
              </div>
              <div className={styles.actorList}>
                {selected.activity.map((item) => (
                  <div key={item.label}>
                    <i />
                    <span><strong>{item.label}</strong><small>{item.detail}</small></span>
                    <b>{item.count}</b>
                  </div>
                ))}
              </div>
            </>
          )}

          {selected.lineage && selected.lineage.length > 0 && (
            <>
              <div className={styles.sectionTitle}>
                <span>SUBJECT LINEAGE</span>
                <small>假设修订 · 不是身份变形</small>
              </div>
              <div className={styles.actorList}>
                {selected.lineage.map((item) => (
                  <div key={`${item.label}:${item.detail}`}>
                    <i />
                    <span><strong>{item.label}</strong><small>{item.detail}</small></span>
                    <b>{item.confidence}%</b>
                  </div>
                ))}
              </div>
            </>
          )}

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
            <h2>
              {importedSubjects
                ? "这个本地关系模型经历了什么？"
                : "A 的小社会是怎样形成的？"}
            </h2>
          </div>
          <div className={styles.storyControls}>
            <button type="button" onClick={() => setStep(Math.max(0, step - 1))} disabled={step === 0}>← 上一步</button>
            <span>{Math.min(step + 1, timelineEvents.length)} / {timelineEvents.length}</span>
            <button type="button" onClick={() => setStep(Math.min(timelineEvents.length - 1, step + 1))} disabled={step >= timelineEvents.length - 1}>下一步 →</button>
          </div>
        </div>

        <p className={styles.activityBoundary}>
          <b>APPEND ORDER</b>
          时间线按本地持久化顺序回放；occurred_ms 只表示来源声称的发生时间。
          导入 v7 模型时，证据引用、正文与决定理由不会进入浏览器投影。
        </p>

        <div className={styles.timeline}>
          {timelineEvents.map((event, index) => (
            <article
              key={event.id}
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

        <section className={styles.disclosureLab}>
          <div className={styles.disclosureHeading}>
            <div>
              <p className={styles.kicker}>REMOTE OBSERVER / RELATIONSHIP DISCLOSURE</p>
              <h3>把 A 的视角给 G 看，不把 A 的判断变成 G 的判断。</h3>
            </div>
            <button
              type="button"
              onClick={() => setDisclosureSent((current) => !current)}
            >
              {disclosureSent ? "撤下本页演示" : "加密披露当前页 →"}
            </button>
          </div>

          <div className={styles.scheduleConsole}>
            <div>
              <small>OBSERVER-LOCAL DISCLOSURE SCHEDULE</small>
              <strong>
                {disclosureSchedule === "active"
                  ? "ACTIVE · A → G · FUTURE ACTIVITY"
                  : disclosureSchedule === "revoked"
                    ? "REVOKED · PENDING BATCH CLEARED"
                    : "NOT CONFIGURED"}
              </strong>
              <p>
                scope: all subjects · interval: 5m · expires: 30d ·
                history replay: off
              </p>
            </div>
            <div>
              {disclosureSchedule !== "active" ? (
                <button
                  type="button"
                  onClick={() => {
                    setDisclosureSchedule("active");
                    setDisclosureSent(false);
                  }}
                >
                  创建持续披露计划
                </button>
              ) : (
                <>
                  <button
                    type="button"
                    onClick={() => setDisclosureSent(true)}
                  >
                    模拟一条新活动
                  </button>
                  <button
                    type="button"
                    className={styles.revokeButton}
                    onClick={() => {
                      setDisclosureSchedule("revoked");
                      setDisclosureSent(false);
                    }}
                  >
                    立即撤销
                  </button>
                </>
              )}
            </div>
          </div>

          <div className={styles.disclosureFlow}>
            <article>
              <small>01 · OBSERVER A</small>
              <strong>A 的本地关系活动</strong>
              <p>
                {Math.min(timelineEvents.length, 100)} 条结构化活动 ·
                append order · evidence digest
              </p>
              <b>LOCAL WORLDVIEW</b>
            </article>
            <i aria-hidden="true">→</i>
            <article className={disclosureSent ? styles.packetSent : ""}>
              <small>02 · ENCRYPTED PACKET</small>
              <strong>只绑定观察者 G</strong>
              <p>
                social.relationship.disclosure · content-free ·
                audience-private
              </p>
              <b>{disclosureSent ? "DELIVERED" : "NOT SENT"}</b>
            </article>
            <i aria-hidden="true">→</i>
            <article className={disclosureSent ? styles.observerReceived : ""}>
              <small>03 · OBSERVER G</small>
              <strong>
                {disclosureSent ? "看见 A 报告的世界" : "尚未收到远端视图"}
              </strong>
              <p>
                G 自己的 Subject / 圈层 / contextual trust：
                <em> 0 项改变</em>
              </p>
              <b>AUTHORIZATION EFFECT: NONE</b>
            </article>
          </div>

          <p className={styles.disclosureBoundary}>
            <b>SEPARATE LEDGER · NO AUDIENCE PULL</b>
            G 收到的是“A 如何看待关系”的披露，不是共同事实，也不是同步后的
            社交图。计划只由 A 创建、限定、过期和撤销；G 不能拉取、扩权或续期。
            人类与 Agent 都可以处于 A 或 G 的位置，角色对调不改变协议。
          </p>
        </section>

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
