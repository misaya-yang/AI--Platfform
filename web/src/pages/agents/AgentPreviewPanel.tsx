import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Input,
  Select,
  Spin,
  Tag,
  Typography,
} from "antd";
import {
  Bot,
  BookOpen,
  ExternalLink,
  MessageSquarePlus,
  RotateCcw,
  Send,
  Trash2,
  User,
  Wrench,
} from "lucide-react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

import {
  agentErrorDetail,
  createDraftPreviewSession,
  createVersionPreviewSession,
  streamAgentPreview,
} from "@/api/agents";
import type {
  AgentRuntimeSession,
  AgentSpec,
  AgentStreamEvent,
  AgentVersion,
} from "@/types/agents";
import {
  agentPreviewEventData,
  agentPreviewEventText,
  agentPreviewToolActivityId,
} from "./agentPreviewEvents";

const { Paragraph, Text, Title } = Typography;

interface PreviewMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
}

interface PreviewActivity {
  id: string;
  kind: "tool" | "knowledge";
  title: string;
  detail: string;
  status?: string;
}

interface PreviewHistory {
  session: AgentRuntimeSession | null;
  messages: PreviewMessage[];
  activities: PreviewActivity[];
  error: string | null;
}

interface AgentPreviewPanelProps {
  agentId: string;
  agentName: string;
  draftRevision: number;
  versions: AgentVersion[];
  savedSpec: AgentSpec;
  dirty: boolean;
}

function eventType(event: AgentStreamEvent): string {
  return String(event.event_type || event.event || "");
}

function previewErrorMessage(error: unknown, t: TFunction): string {
  const detail = agentErrorDetail(error);
  const code = detail.code || "";
  if (code.includes("MODEL")) return t("agents.preview.errors.model", { message: detail.message });
  if (code.includes("SKILL") || code.includes("CAPABILITY") || code.includes("MCP")) {
    return t("agents.preview.errors.capability", { message: detail.message });
  }
  if (code.includes("KNOWLEDGE") || code.includes("DATASET")) {
    return t("agents.preview.errors.knowledge", { message: detail.message });
  }
  if (code.includes("REVISION") || code.includes("VERSION")) {
    return t("agents.preview.errors.configuration", { message: detail.message });
  }
  if (code.includes("FORBIDDEN") || code.includes("PERMISSION") || code.includes("AUTH")) {
    return t("agents.preview.errors.permission", { message: detail.message });
  }
  if (code.includes("PROVIDER")) {
    return t("agents.preview.errors.provider", { message: detail.message });
  }
  if (code.includes("RUNTIME")) {
    return t("agents.preview.errors.runtime", { message: detail.message });
  }
  return detail.message;
}

export function AgentPreviewPanel({
  agentId,
  agentName,
  draftRevision,
  versions,
  savedSpec,
  dirty,
}: AgentPreviewPanelProps) {
  const { t } = useTranslation();
  const [target, setTarget] = useState("draft");
  const [session, setSession] = useState<AgentRuntimeSession | null>(null);
  const [messages, setMessages] = useState<PreviewMessage[]>([]);
  const [activities, setActivities] = useState<PreviewActivity[]>([]);
  const [input, setInput] = useState("");
  const [starting, setStarting] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<Record<string, PreviewHistory>>({});
  const abortRef = useRef<AbortController | null>(null);
  const selectedVersion = useMemo(
    () => versions.find((version) => version.agent_version_id === target),
    [target, versions],
  );
  const effectiveSpec = selectedVersion?.spec ?? savedSpec;
  const memoryMode = String(effectiveSpec.memory.mode || "session");
  const memoryModeLabel = t(`agents.studio.memory.${memoryMode === "user" ? "user" : memoryMode === "off" ? "off" : "session"}`);
  const targetLabel = selectedVersion
    ? t("agents.common.versionLabel", { version: selectedVersion.version_number })
    : t("agents.common.draftLabel", { revision: draftRevision });

  useEffect(() => {
    abortRef.current?.abort();
    setSession(null);
    setMessages([]);
    setActivities([]);
    setError(null);
    // A Draft save bumps draftRevision, but immutable Version previews are
    // unaffected by it — preserve their stored history so returning to a
    // Version target keeps its recoverable transcript (ux-spec Preview
    // contract: 旧预览保留可返回的历史记录). Only the stale Draft entry is
    // dropped; wiping the whole map discarded unrelated Version transcripts.
    setHistory((current) => {
      const next = { ...current };
      delete next.draft;
      return next;
    });
  }, [agentId, draftRevision]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const startSession = async () => {
    setStarting(true);
    setError(null);
    abortRef.current?.abort();
    setMessages([]);
    setActivities([]);
    try {
      const created = selectedVersion
        ? await createVersionPreviewSession(agentId, selectedVersion.agent_version_id)
        : await createDraftPreviewSession(agentId, draftRevision);
      setSession(created);
    } catch (sessionError) {
      setError(previewErrorMessage(sessionError, t));
    } finally {
      setStarting(false);
    }
  };

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setSending(true);
    setError(null);
    let activeSession = session;
    let assistantId: string | null = null;
    try {
      if (!activeSession) {
        activeSession = selectedVersion
          ? await createVersionPreviewSession(agentId, selectedVersion.agent_version_id)
          : await createDraftPreviewSession(agentId, draftRevision);
        setSession(activeSession);
      }
      const userMessage: PreviewMessage = { id: crypto.randomUUID(), role: "user", content: text };
      assistantId = crypto.randomUUID();
      const assistantMessageId = assistantId;
      setMessages((current) => [...current, userMessage, { id: assistantMessageId, role: "assistant", content: "" }]);
      setInput("");
      const controller = new AbortController();
      abortRef.current = controller;
      for await (const event of streamAgentPreview({
        agentId,
        draftRevision,
        versionId: selectedVersion?.agent_version_id,
        sessionId: activeSession.session_id,
        message: text,
        signal: controller.signal,
      })) {
        const type = eventType(event);
        const data = agentPreviewEventData(event);
        if (type === "text_delta" || type === "text_message_content") {
          const delta = agentPreviewEventText(event);
          setMessages((current) => current.map((message) => message.id === assistantId ? { ...message, content: `${message.content}${delta}` } : message));
        } else if (type.includes("tool_call") || type === "tool_result") {
          const name = String(event.tool_name || data.tool_name || data.name || t("agents.preview.toolDefault"));
          const activityId = agentPreviewToolActivityId(event);
          const nextActivity: PreviewActivity = {
            id: activityId || crypto.randomUUID(),
            kind: "tool",
            title: t("agents.preview.toolTitle", { name }),
            detail: t("agents.preview.toolDetail"),
            status: String(event.status || data.status || t("agents.preview.toolStatus")),
          };
          setActivities((current) => {
            if (!activityId) return [...current, nextActivity];
            const existing = current.findIndex((activity) => activity.id === activityId);
            if (existing < 0) return [...current, nextActivity];
            return current.map((activity, index) => index === existing ? nextActivity : activity);
          });
        } else if (type.includes("context") || type.includes("knowledge") || type.includes("citation")) {
          const dataset = String(event.dataset_name || data.dataset_name || data.dataset_id || t("agents.preview.knowledgeDefault"));
          const rawCitationCount = event.citation_count ?? data.citation_count;
          const citationCount = typeof rawCitationCount === "number" && Number.isFinite(rawCitationCount)
            ? Math.max(0, Math.floor(rawCitationCount))
            : 0;
          setActivities((current) => [...current, {
            id: crypto.randomUUID(),
            kind: "knowledge",
            title: t("agents.preview.knowledgeTitle", { dataset }),
            detail: citationCount
              ? t(citationCount === 1 ? "agents.preview.citationOne" : "agents.preview.citationOther", { count: citationCount })
              : t("agents.preview.knowledgeDetail"),
          }]);
        } else if (type === "error" || type === "run_error") {
          throw new Error(agentPreviewEventText(event) || t("agents.preview.runtimeError"));
        }
      }
      setMessages((current) => current.map((message) => message.id === assistantId && !message.content ? { ...message, content: t("agents.preview.emptyResponse") } : message));
    } catch (streamError) {
      const aborted = streamError instanceof DOMException && streamError.name === "AbortError";
      if (!aborted) {
        setError(previewErrorMessage(streamError, t));
      }
      // The stream failed or was aborted before the emptyResponse patch ran, so
      // the placeholder assistant bubble would otherwise show a permanent
      // "Generating…" cursor. Replace its empty content with the empty-response
      // label (or leave any partially streamed text intact).
      if (assistantId) {
        const failedId = assistantId;
        setMessages((current) => current.map((message) => message.id === failedId && !message.content ? { ...message, content: t("agents.preview.emptyResponse") } : message));
      }
    } finally {
      setSending(false);
      abortRef.current = null;
    }
  };

  const clearSession = () => {
    abortRef.current?.abort();
    setSession(null);
    setMessages([]);
    setActivities([]);
    setError(null);
    setHistory((current) => {
      const next = { ...current };
      delete next[target];
      return next;
    });
  };

  const switchTarget = (nextTarget: string) => {
    if (nextTarget === target) return;
    if (!window.confirm(t("agents.preview.switchConfirm"))) return;
    abortRef.current?.abort();
    const currentHistory: PreviewHistory = { session, messages, activities, error };
    const nextHistory = history[nextTarget];
    setHistory((current) => ({ ...current, [target]: currentHistory }));
    setTarget(nextTarget);
    setSession(nextHistory?.session ?? null);
    setMessages(nextHistory?.messages ?? []);
    setActivities(nextHistory?.activities ?? []);
    setError(nextHistory?.error ?? null);
    setSending(false);
  };

  return (
    <section className="agent-preview" aria-labelledby="agent-preview-title" data-testid="agent-preview-panel">
      <header className="agent-preview-header">
        <div>
          <Title id="agent-preview-title" level={3}>{t("agents.common.preview")}</Title>
          <Text type="secondary">{t("agents.preview.subtitle")}</Text>
        </div>
        <Button icon={<MessageSquarePlus size={16} />} onClick={() => void startSession()} loading={starting}>
          {t("agents.preview.newSession")}
        </Button>
      </header>

      <Select
        className="agent-preview-target"
        value={target}
        aria-label={t("agents.preview.targetLabel")}
        onChange={switchTarget}
        options={[
          { value: "draft", label: t("agents.common.draftLabel", { revision: draftRevision }) },
          ...versions.map((version) => ({ value: version.agent_version_id, label: t("agents.common.versionLabel", { version: version.version_number }) })),
        ]}
      />

      <Alert
        className="agent-preview-notice"
        type={dirty ? "warning" : "info"}
        showIcon
        title={dirty
          ? t("agents.preview.dirtyNotice", { target: targetLabel })
          : t("agents.preview.savedNotice", { target: targetLabel })}
      />

      <div className="agent-effective-summary" aria-label={t("agents.preview.summaryLabel")}>
        <span>{t("agents.preview.summaryModel", { model: effectiveSpec.model.model_id || t("agents.common.serverDefault") })}</span>
        <span>{t("agents.preview.summaryCapabilities", { count: effectiveSpec.capabilities.length })}</span>
        <span>{t("agents.preview.summaryKnowledge", { count: effectiveSpec.knowledge.length })}</span>
        <span>{t("agents.preview.summaryMemory", { mode: memoryModeLabel })}</span>
      </div>

      <div className="agent-preview-transcript" aria-live="polite">
        {!session && messages.length === 0 && !starting && (
          <div className="agent-preview-empty">
            <Bot size={28} />
            <Title level={4}>{t("agents.preview.startTitle")}</Title>
            <Paragraph type="secondary">{t("agents.preview.startDescription", { target: targetLabel })}</Paragraph>
            <Button type="primary" onClick={() => void startSession()}>{t("agents.preview.startButton")}</Button>
          </div>
        )}
        {starting && <div className="agent-preview-spinner"><Spin /><span>{t("agents.preview.resolving")}</span></div>}
        {session && <div className="agent-session-label"><RotateCcw size={14} /> {t("agents.preview.sessionLabel", { target: targetLabel })}</div>}
        {messages.map((message) => (
          <article key={message.id} className={`agent-preview-message agent-preview-message-${message.role}`}>
            <span className="agent-message-avatar" aria-hidden>{message.role === "user" ? <User size={15} /> : <Bot size={15} />}</span>
            <div><strong>{message.role === "user" ? t("agents.preview.you") : agentName}</strong><p>{message.content || <span className="agent-stream-cursor">{t("agents.preview.generating")}</span>}</p></div>
          </article>
        ))}
        {activities.map((activity) => (
          <article key={activity.id} className="agent-preview-activity">
            {activity.kind === "tool" ? <Wrench size={15} /> : <BookOpen size={15} />}
            <div><strong>{activity.title}</strong><p>{activity.detail}</p></div>
            {activity.status && <Tag color="green">{activity.status}</Tag>}
          </article>
        ))}
      </div>

      {error && <Alert className="agent-preview-error" type="error" showIcon title={t("agents.preview.failed")} description={error} action={<Button onClick={() => void startSession()}>{t("agents.preview.newSession")}</Button>} />}

      <div className="agent-preview-composer">
        <Input.TextArea
          autoSize={{ minRows: 1, maxRows: 5 }}
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onPressEnter={(event) => {
            if (!event.shiftKey) {
              event.preventDefault();
              void sendMessage();
            }
          }}
          placeholder={t("agents.preview.messagePlaceholder")}
          aria-label={t("agents.preview.messagePlaceholder")}
          disabled={starting}
        />
        <Button type="primary" icon={<Send size={16} />} aria-label={t("agents.preview.sendLabel")} disabled={!input.trim() || starting} loading={sending} onClick={() => void sendMessage()} />
      </div>
      <footer className="agent-preview-footer">
        <Button type="text" icon={<Trash2 size={14} />} onClick={clearSession} disabled={!session && messages.length === 0}>{t("agents.preview.clear")}</Button>
        {session ? <Link to={`/eval?tab=traces&family=assistant&session_id=${encodeURIComponent(session.session_id)}`}>{t("agents.preview.openTrace")} <ExternalLink size={13} /></Link> : <Text type="secondary">{t("agents.preview.traceAfterRun")}</Text>}
      </footer>
    </section>
  );
}
