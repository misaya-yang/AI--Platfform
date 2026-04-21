/**
 * Chat Message Component
 *
 * Renders a single message in the chat. Reasoning, tool calls, search status
 * and the thinking stream are consolidated into a single Claude.ai-style
 * "Activity" chip that opens a side panel (ActivityPanel) — replacing the
 * previous stacked ProcessSummaryBar / ToolCallsDisplay / ThinkingPanel /
 * SearchStatusDisplay / WebSearchDisplay / Thought Process block.
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { motion } from "framer-motion";
import {
  Clock,
  MessageSquare,
  FileText,
  Image as ImageIcon,
  Loader2,
  CheckCircle2,
  Zap,
  Brain,
  PenTool,
  Cog,
  Eye,
  ListTodo,
  ListTree,
  ChevronDown,
  Download,
  ExternalLink,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { StreamOutput } from "@/components/StreamOutput";
import { SubAgentCard } from "./SubAgentCard";
import { ContextDisplay } from "./ContextDisplay";
import { CitationDisplay } from "./CitationDisplay";
import { DocumentPreview } from "./DocumentPreview";
import type { ChatMessage as ChatMessageType, AgentPhaseStatus } from "../types";
import { QuizCard } from "./Quiz";
import { ActivityPanel } from "./ActivityPanel";
import { buildTimeline } from "./buildTimeline";

interface ChatMessageProps {
  message: ChatMessageType;
}

const ASSISTANT_UI_V2 = import.meta.env.VITE_ASSISTANT_UI_V2 !== "false";

function InlineArtifactCard({
  artifact,
}: {
  artifact: NonNullable<ChatMessageType["generatedArtifacts"]>[number];
}) {
  const { t } = useTranslation();
  const hasUrl = Boolean(artifact.url);
  const format = (artifact.format || "file").toUpperCase();
  const title = artifact.title || artifact.filename || "Artifact";
  const meta = [format, artifact.sizeBytes ? `${Math.round(artifact.sizeBytes / 1024)} KB` : null]
    .filter(Boolean)
    .join(" · ");

  // Only show "Open in new tab" for browser-previewable formats
  const previewable = /^(png|jpg|jpeg|gif|webp|svg|pdf|md|txt|html|json|csv)$/i.test(artifact.format || "");

  return (
    <div className="rounded-xl border border-[hsl(var(--assistant-border-soft))] bg-[hsl(var(--assistant-chip-bg))]/60 p-3">
      <div className="flex items-center gap-2">
        <FileText className="h-4 w-4 text-[hsl(var(--assistant-accent))]" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-[hsl(var(--assistant-text-primary))] truncate">{title}</p>
          <p className="text-[11px] text-[hsl(var(--assistant-text-secondary))]">{meta}</p>
        </div>
        {hasUrl && (
          <>
            {previewable && (
              <a
                href={artifact.url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 rounded-md border border-[hsl(var(--assistant-border-soft))] px-2 py-1 text-[11px] text-[hsl(var(--assistant-text-secondary))] hover:text-[hsl(var(--assistant-text-primary))]"
              >
                <ExternalLink className="h-3 w-3" />
                {t("common.openInNewTab", "新标签页打开")}
              </a>
            )}
            <a
              href={artifact.url}
              download={artifact.filename || title}
              className="inline-flex items-center gap-1 rounded-md border border-[hsl(var(--assistant-border-soft))] px-2 py-1 text-[11px] text-[hsl(var(--assistant-text-secondary))] hover:text-[hsl(var(--assistant-text-primary))]"
            >
              <Download className="h-3 w-3" />
              {t("artifact.download", "下载")}
            </a>
          </>
        )}
      </div>
    </div>
  );
}

/** Agent phase display - Manus style compact indicator */
function AgentPhaseDisplay({ phase }: { phase: AgentPhaseStatus }) {
  const { t } = useTranslation();

  const phaseConfig: Record<
    string,
    { icon: React.ReactNode; color: string; bgColor: string; label: string }
  > = {
    analyzing: {
      icon: <Brain className="h-3 w-3" />,
      color: "text-blue-600 dark:text-blue-400",
      bgColor: "bg-blue-100 dark:bg-blue-900/40",
      label: t("assistant.phase.analyzing"),
    },
    thinking: {
      icon: <Brain className="h-3 w-3" />,
      color: "text-violet-600 dark:text-violet-400",
      bgColor: "bg-violet-100 dark:bg-violet-900/40",
      label: t("assistant.phase.thinking"),
    },
    planning: {
      icon: <ListTodo className="h-3 w-3" />,
      color: "text-amber-600 dark:text-amber-400",
      bgColor: "bg-amber-100 dark:bg-amber-900/40",
      label: t("assistant.phase.planning"),
    },
    executing: {
      icon: <Cog className="h-3 w-3" />,
      color: "text-emerald-600 dark:text-emerald-400",
      bgColor: "bg-emerald-100 dark:bg-emerald-900/40",
      label: t("assistant.phase.executing"),
    },
    observing: {
      icon: <Eye className="h-3 w-3" />,
      color: "text-cyan-600 dark:text-cyan-400",
      bgColor: "bg-cyan-100 dark:bg-cyan-900/40",
      label: t("assistant.phase.observing"),
    },
    writing: {
      icon: <PenTool className="h-3 w-3" />,
      color: "text-pink-600 dark:text-pink-400",
      bgColor: "bg-pink-100 dark:bg-pink-900/40",
      label: t("assistant.phase.writing"),
    },
    completing: {
      icon: <CheckCircle2 className="h-3 w-3" />,
      color: "text-emerald-600 dark:text-emerald-400",
      bgColor: "bg-emerald-100 dark:bg-emerald-900/40",
      label: t("assistant.phase.completing"),
    },
  };

  const config = phaseConfig[phase.phase] || phaseConfig.thinking;

  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      className="flex items-center gap-2 mt-2"
    >
      <div className={cn("flex items-center justify-center w-5 h-5 rounded-md", config.bgColor)}>
        <motion.div
          animate={phase.phase === "executing" ? { rotate: 360 } : {}}
          transition={{ duration: 2, repeat: Infinity, ease: "linear" }}
          className={config.color}
        >
          {config.icon}
        </motion.div>
      </div>
      <span className={cn("text-xs font-medium", config.color)}>
        {phase.message || config.label}
      </span>
      <div className="flex gap-0.5">
        {[0, 1, 2].map((i) => (
          <motion.span
            key={i}
            className={cn("w-1 h-1 rounded-full", config.color.replace("text-", "bg-"))}
            animate={{ opacity: [0.3, 1, 0.3] }}
            transition={{ duration: 1, repeat: Infinity, delay: i * 0.15 }}
          />
        ))}
      </div>
    </motion.div>
  );
}

/** Stats badge showing token usage and timing */
function StatsBadge({ message }: { message: ChatMessageType }) {
  const { t } = useTranslation();
  if (!message.usage && !message.durationMs) return null;

  const totalTokens =
    (message.usage?.input_tokens || 0) + (message.usage?.output_tokens || 0);

  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.2 }}
      className="flex flex-wrap items-center gap-2 text-[10px] mt-3"
    >
      {message.durationMs != null && (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-slate-100 dark:bg-slate-800/60 text-slate-600 dark:text-slate-400">
          <Clock className="h-3 w-3" />
          {(message.durationMs / 1000).toFixed(2)}s
        </span>
      )}
      {message.firstTokenMs != null && (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-slate-100 dark:bg-slate-800/60 text-slate-600 dark:text-slate-400">
          <Zap className="h-3 w-3" />
          {t("playground.stats.ttft", "TTFT")}: {message.firstTokenMs}ms
        </span>
      )}
      {totalTokens > 0 && (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-slate-100 dark:bg-slate-800/60 text-slate-600 dark:text-slate-400">
          <MessageSquare className="h-3 w-3" />
          {totalTokens} {t("assistant.tokens", "tokens")}
          {message.usage?.input_tokens != null &&
            message.usage?.output_tokens != null && (
              <span className="text-slate-400 dark:text-slate-500 ml-1">
                ({message.usage.input_tokens}↑ / {message.usage.output_tokens}↓)
              </span>
            )}
        </span>
      )}
      {message.cacheMetrics && message.cacheMetrics.cache_hit_rate > 0 && (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-900/40 text-emerald-600 dark:text-emerald-400">
          <Zap className="h-3 w-3" />
          {(message.cacheMetrics.cache_hit_rate * 100).toFixed(0)}% cached
          {message.cacheMetrics.estimated_savings_usd > 0.0001 && (
            <span className="text-emerald-500 ml-1">
              (-${message.cacheMetrics.estimated_savings_usd.toFixed(4)})
            </span>
          )}
        </span>
      )}
    </motion.div>
  );
}

/** Attachments display in message */
function AttachmentsDisplay({
  attachments,
  useV2,
}: {
  attachments: ChatMessageType["attachments"];
  useV2: boolean;
}) {
  if (!attachments || attachments.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-2 mb-3">
      {attachments.map((att, idx) => (
        <div
          key={idx}
          className={
            useV2
              ? "flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs border border-[hsl(var(--assistant-border-soft))] bg-[hsl(var(--assistant-chip-bg))]/80 text-[hsl(var(--assistant-text-secondary))]"
              : "flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-white/20 backdrop-blur-sm text-xs text-white/90 border border-white/20"
          }
        >
          {att.type === "image" ? (
            <ImageIcon className="h-3.5 w-3.5" />
          ) : (
            <FileText className="h-3.5 w-3.5" />
          )}
          <span className="truncate max-w-[100px]">
            {att.filename || "Attachment"}
          </span>
        </div>
      ))}
    </div>
  );
}

/** Compact image generation placeholder */
function ImageGeneratingPlaceholder({ prompt }: { prompt?: string }) {
  const { t } = useTranslation();

  return (
    <div className="space-y-2">
      <motion.div
        initial={{ opacity: 0, scale: 0.98 }}
        animate={{ opacity: 1, scale: 1 }}
        className="relative w-[200px] h-[200px] rounded-xl overflow-hidden bg-gradient-to-br from-slate-100 to-slate-200 dark:from-slate-800 dark:to-slate-700"
      >
        <motion.div
          className="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent dark:via-white/5"
          animate={{ x: ["-100%", "100%"] }}
          transition={{ duration: 1.5, repeat: Infinity, ease: "linear" }}
        />
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-3">
          <motion.div
            animate={{ scale: [1, 1.05, 1] }}
            transition={{ duration: 2, repeat: Infinity, ease: "easeInOut" }}
            className="p-3 rounded-lg bg-pink-500/10"
          >
            <ImageIcon className="h-8 w-8 text-pink-500" />
          </motion.div>
          <div className="flex items-center gap-1.5">
            <span className="text-xs font-medium text-slate-500 dark:text-slate-400">
              {t("assistant.creatingImage")}
            </span>
            <div className="flex gap-0.5">
              {[0, 1, 2].map((i) => (
                <motion.div
                  key={i}
                  className="w-1 h-1 rounded-full bg-pink-500"
                  animate={{ opacity: [0.3, 1, 0.3] }}
                  transition={{ duration: 1, repeat: Infinity, delay: i * 0.15 }}
                />
              ))}
            </div>
          </div>
        </div>
      </motion.div>
      {prompt && (
        <p className="text-[11px] text-slate-400 dark:text-slate-500 truncate max-w-[200px]">
          "{prompt.length > 40 ? prompt.slice(0, 40) + "..." : prompt}"
        </p>
      )}
    </div>
  );
}

function formatDurationLabel(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(seconds >= 10 ? 0 : 1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

/** Claude.ai-style compact activity chip: "Activity · N steps · Ts" */
function ActivityChip({
  message,
  totalDurationMs,
  stepCount,
  isOpen,
  onToggle,
}: {
  message: ChatMessageType;
  totalDurationMs: number;
  stepCount: number;
  isOpen: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const isStreaming = !!message.isStreaming;

  // Live-elapsed ticker while streaming
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!isStreaming) return;
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
  }, [isStreaming]);

  const createdMs = message.createdAt ? new Date(message.createdAt).getTime() : undefined;
  const liveElapsed = isStreaming && createdMs ? Math.max(0, now - createdMs) : 0;
  const effectiveMs = isStreaming
    ? Math.max(totalDurationMs, liveElapsed)
    : totalDurationMs;

  const durationLabel = formatDurationLabel(effectiveMs);
  const stepsText = t("playground.activity.steps", {
    count: stepCount,
    defaultValue: "{{count}} steps",
  });

  const label = durationLabel
    ? t("playground.activity.triggerWithDuration", {
        count: stepCount,
        steps: stepsText,
        duration: durationLabel,
        defaultValue: "Activity · {{steps}} · {{duration}}",
      })
    : t("playground.activity.trigger", {
        count: stepCount,
        steps: stepsText,
        defaultValue: "Activity · {{steps}}",
      });

  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={isOpen}
      className={cn(
        "group inline-flex items-center gap-2 rounded-full border px-3 py-1.5",
        "border-slate-200 dark:border-slate-700/80",
        isOpen
          ? "bg-slate-100 dark:bg-slate-800/70 text-slate-900 dark:text-slate-100"
          : "bg-white/60 dark:bg-slate-900/50 backdrop-blur-sm text-slate-600 dark:text-slate-300",
        "text-[12px]",
        "hover:bg-slate-50 dark:hover:bg-slate-800/70 hover:text-slate-900 dark:hover:text-slate-100",
        "transition-colors",
      )}
      aria-label={label}
    >
      {isStreaming ? (
        <Loader2 className="h-3.5 w-3.5 animate-spin text-[hsl(var(--assistant-accent))]" />
      ) : (
        <ListTree className="h-3.5 w-3.5 text-slate-500 dark:text-slate-400 group-hover:text-slate-700 dark:group-hover:text-slate-200" />
      )}
      <span className="font-medium">{label}</span>
      <ChevronDown
        className={cn(
          "h-3.5 w-3.5 text-slate-400 transition-transform duration-200",
          isOpen && "rotate-180",
        )}
      />
    </button>
  );
}

export function ChatMessage({ message }: ChatMessageProps) {
  const { t } = useTranslation();
  const isUser = message.role === "user";
  const [activityOpen, setActivityOpen] = useState(false);

  // Pre-compute the timeline so we can decide whether to show the chip at all
  // and to pass totals to the panel/chip. Safe for user messages too — builder
  // returns 0 steps for non-assistant turns.
  const { steps: timelineSteps, totalDurationMs } = useMemo(
    () => (isUser ? { steps: [], totalDurationMs: 0 } : buildTimeline(message, t)),
    [message, t, isUser],
  );

  const hasActivity =
    !isUser && (timelineSteps.length > 0 || !!message.isStreaming);

  // Show a "thinking" 3-dot placeholder only if nothing else (no activity, no content)
  // has landed yet — i.e. we literally have no signal to show.
  const showTypingDots =
    !isUser &&
    !!message.isStreaming &&
    !message.content &&
    timelineSteps.length === 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.25, 0.1, 0.25, 1] }}
      className={cn("group w-full", isUser ? "flex justify-end" : "")}
    >
      {isUser ? (
        <div className="max-w-[75%]">
          <AttachmentsDisplay attachments={message.attachments} useV2={ASSISTANT_UI_V2} />
          <div
            className={cn(
              "rounded-3xl px-5 py-3",
              ASSISTANT_UI_V2
                ? "bg-[hsl(var(--assistant-user-bubble))] text-[hsl(var(--assistant-text-primary))]"
                : "bg-primary text-white shadow-sm shadow-primary/15",
            )}
          >
            <div className="whitespace-pre-wrap leading-relaxed text-[15px]">
              {message.content}
            </div>
          </div>
        </div>
      ) : (
        /* Assistant message — clean, no bubble, full width */
        <div className="w-full space-y-3 assistant-copy pl-1">
          {/* Activity chip + inline-expandable panel (replaces the old stack
              of ProcessSummaryBar / ToolCallsDisplay / ThinkingPanel /
              SearchStatusDisplay / WebSearchDisplay / Thought Process).
              Panel is rendered inline here — NOT as a right-side sheet —
              because the artifacts panel already owns the right rail. */}
          {hasActivity && (
            <div>
              <ActivityChip
                message={message}
                totalDurationMs={totalDurationMs}
                stepCount={timelineSteps.length}
                isOpen={activityOpen}
                onToggle={() => setActivityOpen((v) => !v)}
              />
              <ActivityPanel
                open={activityOpen}
                onOpenChange={setActivityOpen}
                message={message}
                totalDurationMs={totalDurationMs}
              />
            </div>
          )}

          {/* Typing dots: only if we have no signal at all yet */}
          {showTypingDots && (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="flex items-center gap-2.5 py-2"
            >
              <div className="flex gap-1">
                {[0, 1, 2].map((i) => (
                  <motion.div
                    key={i}
                    className="w-1.5 h-1.5 rounded-full bg-[hsl(var(--assistant-accent))]"
                    animate={{ opacity: [0.3, 1, 0.3] }}
                    transition={{
                      duration: 1.4,
                      repeat: Infinity,
                      delay: i * 0.2,
                      ease: "easeInOut",
                    }}
                  />
                ))}
              </div>
            </motion.div>
          )}

          {/* Context display — still shown inline (not noise; it's citation source detail) */}
          {message.contexts && message.contexts.length > 0 && (
            <ContextDisplay contexts={message.contexts} />
          )}

          {/* Sub-Agent Cards (ADR-003) */}
          {message.activeSubAgents && message.activeSubAgents.length > 0 && (
            <div className="mb-3 space-y-1">
              {message.activeSubAgents.map((sa) => (
                <SubAgentCard key={sa.agentId} subAgent={sa} />
              ))}
            </div>
          )}

          {/* Message content */}
          {message.isGeneratingImage ? (
            <ImageGeneratingPlaceholder prompt={message.imageGenerationPrompt} />
          ) : message.content ? (
            <div className="text-[hsl(var(--assistant-text-primary))] text-[15px] leading-[1.75]">
              <StreamOutput
                text={message.content}
                isStreaming={!!message.isStreaming}
                id={`msg-${message.id}`}
              />
            </div>
          ) : !showTypingDots && !message.isStreaming && (
            <span className="text-[hsl(var(--assistant-text-secondary))] italic text-sm">
              {t("assistant.emptyResponse", "(No response)")}
            </span>
          )}

          {/* Agent phase display */}
          {message.isStreaming && message.agentPhase && (
            <AgentPhaseDisplay phase={message.agentPhase} />
          )}

          {/* Citation display */}
          {!message.isStreaming && message.ragCitations && message.ragCitations.length > 0 && (
            <CitationDisplay
              citations={message.ragCitations}
              evaluation={message.ragEvaluation}
            />
          )}

          {/* Generated artifacts */}
          {!message.isStreaming &&
            message.generatedArtifacts &&
            message.generatedArtifacts.length > 0 &&
            (ASSISTANT_UI_V2 ? (
              <div className="mt-4 space-y-2">
                {message.generatedArtifacts.map((artifact) => (
                  <InlineArtifactCard key={artifact.id} artifact={artifact} />
                ))}
              </div>
            ) : (
              <div className="mt-4 space-y-3">
                {message.generatedArtifacts.map((artifact) => (
                  <DocumentPreview
                    key={artifact.id}
                    title={artifact.title || artifact.filename || "Document"}
                    content={artifact.content || ""}
                    format={
                      artifact.format === "md" || artifact.format === "markdown"
                        ? "markdown"
                        : "text"
                    }
                    downloadUrl={artifact.url}
                    defaultExpanded={false}
                    maxHeight={300}
                  />
                ))}
              </div>
            ))}

          {/* Quiz card */}
          {message.quizData && (
            <div className="mt-4">
              <QuizCard quizData={message.quizData} existingResult={message.quizResult} />
            </div>
          )}

          {/* Stats */}
          {!message.isStreaming && <StatsBadge message={message} />}
        </div>
      )}
    </motion.div>
  );
}
