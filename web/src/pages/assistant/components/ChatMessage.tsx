/**
 * Chat Message Component
 *
 * Renders a single message in the chat, with support for:
 * - User/assistant avatars
 * - Streaming content
 * - File attachments
 * - KB context display
 * - Web search results
 * - Usage statistics
 * - Search status display (Phase 1)
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import {
  Bot,
  User,
  Clock,
  MessageSquare,
  FileText,
  Image as ImageIcon,
  Database,
  Globe,
  Loader2,
  CheckCircle2,
  AlertCircle,
  Zap,
  Brain,
  PenTool,
  Cog,
  Eye,
  ListTodo,
  ChevronRight,
  ChevronDown,
  Search,
  Download,
  ExternalLink,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { StreamOutput } from "@/components/StreamOutput";
import { SubAgentCard } from "./SubAgentCard";
import { WebSearchDisplay } from "./WebSearchDisplay";
import { ContextDisplay } from "./ContextDisplay";
import { CitationDisplay } from "./CitationDisplay";
import { DocumentPreview } from "./DocumentPreview";
import type { ChatMessage as ChatMessageType, SearchStatusItem, AgentPhaseStatus } from "../types";
import { ProcessSummaryBar } from "./ProcessSummaryBar";
import { ThinkingPanel } from "./ThinkingPanel";
import { QuizCard } from "./Quiz";

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

/** Manus-style collapsible task panel */
function SearchStatusDisplay({ searchStatus }: { searchStatus: SearchStatusItem[] }) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(!ASSISTANT_UI_V2);

  if (!searchStatus || !Array.isArray(searchStatus) || searchStatus.length === 0) return null;

  const allCompleted = searchStatus.every(item => item.state === "completed");
  const completedCount = searchStatus.filter(item => item.state === "completed").length;
  const totalCount = searchStatus.length;
  const hasError = searchStatus.some((item) => item.state === "error");
  const shouldExpand = hasError ? true : isExpanded;

  const getStepConfig = (item: SearchStatusItem) => {
    const isKB = item.type === "kb";
    const isFiles = item.type === "files";
    const isSearching = item.state === "searching";
    const isCompleted = item.state === "completed";

    // Get action text (what's being done)
    const actionText = isSearching
      ? isKB
        ? t("assistant.searchingKB")
        : isFiles
        ? t("assistant.processingFiles")
        : t("assistant.searchingWeb")
      : isCompleted
      ? isKB
        ? t("assistant.readSources", { count: item.resultCount || 0 })
        : isFiles
        ? t("assistant.analyzedFiles", { count: item.resultCount || 0 })
        : t("assistant.foundResults", { count: item.resultCount || 0 })
      : item.error || t("assistant.searchFailed");

    return {
      icon: isKB ? Database : isFiles ? FileText : Search,
      iconColor: isKB
        ? "text-emerald-500"
        : isFiles
        ? "text-violet-500"
        : "text-blue-500",
      actionText: actionText || "",
      isSearching,
      isCompleted,
    };
  };

  const headerLabel = hasError
    ? t("assistant.searchFailed")
    : allCompleted
      ? t("assistant.researchComplete")
      : t("assistant.researching");

  if (ASSISTANT_UI_V2) {
    return (
      <div className="mb-2">
        <button
          type="button"
          onClick={() => setIsExpanded((prev) => !prev)}
          className="w-full py-1.5 pr-1 flex items-center gap-2 text-left"
          aria-expanded={shouldExpand}
        >
          {allCompleted ? (
            <CheckCircle2 className="h-4 w-4 text-emerald-500 shrink-0" />
          ) : (
            <Loader2 className="h-4 w-4 text-[hsl(var(--assistant-accent))] animate-spin shrink-0" />
          )}
          <span className="text-xs sm:text-sm text-[hsl(var(--assistant-text-secondary))] flex-1 truncate">
            {headerLabel} {completedCount}/{totalCount}
          </span>
          <motion.span animate={{ rotate: shouldExpand ? 180 : 0 }} transition={{ duration: 0.15 }}>
            <ChevronDown className="h-4 w-4 text-[hsl(var(--assistant-text-secondary))]" />
          </motion.span>
        </button>

        <AnimatePresence initial={false}>
          {shouldExpand && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.18 }}
              className="overflow-hidden"
            >
              <div className="ml-5 pl-3 pb-2 border-l border-[hsl(var(--assistant-border-soft))]/80 space-y-1.5">
                {searchStatus.map((item, index) => {
                  const config = getStepConfig(item);
                  const IconComponent = config.icon;
                  return (
                    <motion.div
                      key={`${item.type}-${index}`}
                      initial={{ opacity: 0, x: -8 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ delay: index * 0.04 }}
                      className="flex items-center gap-2 text-xs"
                    >
                      {config.isSearching ? (
                        <Loader2 className={cn("h-3.5 w-3.5 animate-spin", config.iconColor)} />
                      ) : config.isCompleted ? (
                        <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                      ) : (
                        <AlertCircle className="h-3.5 w-3.5 text-red-500" />
                      )}
                      <IconComponent className={cn("h-3 w-3", config.iconColor)} />
                      <span className="flex-1 min-w-0 truncate text-[hsl(var(--assistant-text-primary))]">
                        {config.actionText}
                      </span>
                      {!config.isSearching && item.durationMs !== undefined && (
                        <span className="text-[10px] text-[hsl(var(--assistant-text-secondary))] font-mono">
                          {(item.durationMs / 1000).toFixed(1)}s
                        </span>
                      )}
                    </motion.div>
                  );
                })}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    );
  }

  return (
    <div className="mb-4">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        className="rounded-2xl border border-slate-200/80 dark:border-slate-700/60 bg-white/80 dark:bg-slate-800/60 backdrop-blur-sm overflow-hidden shadow-sm"
      >
        {/* Clickable header - Manus style */}
        <button
          onClick={() => setIsExpanded(!isExpanded)}
          className="w-full px-4 py-3 flex items-center gap-3 hover:bg-slate-50/50 dark:hover:bg-slate-700/30 transition-colors"
        >
          {/* Status icon */}
          <div className={cn(
            "flex-shrink-0 w-6 h-6 rounded-full flex items-center justify-center",
            allCompleted
              ? "bg-emerald-100 dark:bg-emerald-900/40"
              : "bg-blue-100 dark:bg-blue-900/40"
          )}>
            {allCompleted ? (
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
            ) : (
              <Loader2 className="h-3.5 w-3.5 text-blue-500 animate-spin" />
            )}
          </div>

          {/* Title */}
          <div className="flex-1 text-left">
            <span className="text-sm font-medium text-slate-700 dark:text-slate-200">
              {allCompleted
                ? t("assistant.researchComplete")
                : t("assistant.researching")}
            </span>
            <span className="ml-2 text-xs text-slate-400 dark:text-slate-500">
              {completedCount}/{totalCount}
            </span>
          </div>

          {/* Expand/collapse chevron */}
          <motion.div
            animate={{ rotate: isExpanded ? 180 : 0 }}
            transition={{ duration: 0.2 }}
          >
            <ChevronDown className="h-4 w-4 text-slate-400" />
          </motion.div>
        </button>

        {/* Collapsible task list */}
        <AnimatePresence initial={false}>
          {isExpanded && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="overflow-hidden"
            >
              <div className="px-4 pb-3 space-y-1">
                {searchStatus.map((item, index) => {
                  const config = getStepConfig(item);
                  const IconComponent = config.icon;

                  return (
                    <motion.div
                      key={`${item.type}-${index}`}
                      initial={{ opacity: 0, x: -8 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ delay: index * 0.05 }}
                      className="flex items-center gap-3 py-2 pl-2 rounded-lg hover:bg-slate-50/50 dark:hover:bg-slate-700/20 transition-colors"
                    >
                      {/* Step icon */}
                      <div className="flex-shrink-0 w-5 h-5 flex items-center justify-center">
                        {config.isSearching ? (
                          <Loader2 className={cn("h-4 w-4 animate-spin", config.iconColor)} />
                        ) : config.isCompleted ? (
                          <CheckCircle2 className="h-4 w-4 text-emerald-500" />
                        ) : (
                          <IconComponent className="h-4 w-4 text-red-500" />
                        )}
                      </div>

                      {/* Action icon */}
                      <IconComponent className={cn("h-3.5 w-3.5 flex-shrink-0", config.iconColor)} />

                      {/* Action text */}
                      <span className={cn(
                        "text-sm flex-1",
                        config.isSearching
                          ? "text-slate-600 dark:text-slate-300"
                          : config.isCompleted
                          ? "text-slate-500 dark:text-slate-400"
                          : "text-red-600 dark:text-red-400"
                      )}>
                        {config.actionText}
                      </span>

                      {/* Duration badge */}
                      {!config.isSearching && item.durationMs !== undefined && (
                        <span className="text-[10px] text-slate-400 dark:text-slate-500 font-mono">
                          {(item.durationMs / 1000).toFixed(1)}s
                        </span>
                      )}
                    </motion.div>
                  );
                })}
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  );
}

/** Agent phase display - Manus style compact indicator */
function AgentPhaseDisplay({ phase }: { phase: AgentPhaseStatus }) {
  const { t } = useTranslation();

  // Map phase to icon, color, and label
  const phaseConfig: Record<string, { icon: React.ReactNode; color: string; bgColor: string; label: string }> = {
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
      {/* Icon with background */}
      <div className={cn("flex items-center justify-center w-5 h-5 rounded-md", config.bgColor)}>
        <motion.div
          animate={phase.phase === "executing" ? { rotate: 360 } : {}}
          transition={{ duration: 2, repeat: Infinity, ease: "linear" }}
          className={config.color}
        >
          {config.icon}
        </motion.div>
      </div>

      {/* Status text */}
      <span className={cn("text-xs font-medium", config.color)}>
        {phase.message || config.label}
      </span>

      {/* Dots animation */}
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
      {/* Cache metrics */}
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

/** Single Tool Call Card - Expandable Manus style */
function ToolCallCard({ tc, idx }: { tc: NonNullable<ChatMessageType["toolCalls"]>[number]; idx: number }) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(false);

  // Map tool names to icons
  const getToolIcon = (name: string) => {
    const iconMap: Record<string, React.ReactNode> = {
      retrieve_product_info: <Database className="h-4 w-4" />,
      get_product_details: <FileText className="h-4 w-4" />,
      search_kb: <Database className="h-4 w-4" />,
      search_knowledge_base: <Database className="h-4 w-4" />,
      web_search: <Globe className="h-4 w-4" />,
      search_web: <Globe className="h-4 w-4" />,
      execute_python_code: <Cog className="h-4 w-4" />,
      generate_image: <ImageIcon className="h-4 w-4" />,
      generate_pptx: <FileText className="h-4 w-4" />,
      generate_docx: <FileText className="h-4 w-4" />,
    };
    return iconMap[name] || <Cog className="h-4 w-4" />;
  };

  const getStatusConfig = (status: string) => {
    switch (status) {
      case "completed":
        return {
          label: t("assistant.toolStatus.completed"),
          dot: "bg-emerald-500"
        };
      case "running":
        return {
          label: t("assistant.toolStatus.running"),
          dot: "bg-blue-500 animate-pulse"
        };
      case "error":
        return {
          label: t("assistant.toolStatus.error"),
          dot: "bg-red-500"
        };
      default:
        return {
          label: t("assistant.toolStatus.pending"),
          dot: "bg-slate-500"
        };
    }
  };

  const statusConfig = getStatusConfig(tc.status);
  const hasExpandableContent = tc.arguments && Object.keys(tc.arguments).length > 0;

  return (
    <motion.div
      key={tc.id || idx}
      initial={{ opacity: 0, x: -8 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay: idx * 0.04 }}
      className="text-xs"
    >
      <button
        type="button"
        onClick={() => hasExpandableContent && setIsExpanded((prev) => !prev)}
        className={cn(
          "w-full flex items-center gap-2 text-left py-1",
          !hasExpandableContent && "cursor-default"
        )}
        aria-expanded={hasExpandableContent ? isExpanded : undefined}
      >
        <span className={cn("h-1.5 w-1.5 rounded-full", statusConfig.dot)} />
        <span className="text-[hsl(var(--assistant-text-secondary))]">
          {getToolIcon(tc.name)}
        </span>
        <code className="font-mono text-[11px] text-[hsl(var(--assistant-text-primary))] truncate">
          {tc.name}
        </code>
        <span className="ml-auto text-[10px] text-[hsl(var(--assistant-text-secondary))]">
          {statusConfig.label}
        </span>
        {hasExpandableContent && (
          <motion.div animate={{ rotate: isExpanded ? 90 : 0 }} transition={{ duration: 0.15 }}>
            <ChevronRight className="h-3.5 w-3.5 text-[hsl(var(--assistant-text-secondary))]" />
          </motion.div>
        )}
      </button>

      <AnimatePresence>
        {isExpanded && hasExpandableContent && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="overflow-hidden"
          >
            <div className="ml-4 pl-3 py-1 border-l border-[hsl(var(--assistant-border-soft))]/80 space-y-1">
              {/* Full arguments */}
              {tc.arguments && Object.keys(tc.arguments).length > 0 && (
                <div className="space-y-1">
                  <div className="text-[10px] font-medium text-[hsl(var(--assistant-text-secondary))] uppercase tracking-wide">
                    {t("assistant.toolArguments")}
                  </div>
                  <pre className="text-[10px] font-mono text-[hsl(var(--assistant-text-secondary))] overflow-x-auto whitespace-pre-wrap break-all max-h-32">
                    {JSON.stringify(tc.arguments, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

/** GPT-style Tool Calls Display - Default expanded */
function ToolCallsDisplay({ toolCalls, isStreaming }: { toolCalls: ChatMessageType["toolCalls"], isStreaming?: boolean }) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(false);

  if (!toolCalls || toolCalls.length === 0) return null;

  const allCompleted = toolCalls.every(tc => tc.status === "completed");
  const runningCount = toolCalls.filter(tc => tc.status === "running").length;
  const pendingCount = toolCalls.filter(tc => tc.status === "pending").length;
  const hasError = toolCalls.some((tc) => tc.status === "error");
  const isProcessing = isStreaming && !allCompleted;
  const expanded = hasError ? true : isExpanded;

  return (
    <div className="mb-2">
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full py-1.5 pr-1 flex items-center gap-2 text-left"
        aria-expanded={expanded}
      >
        {allCompleted ? (
          <CheckCircle2 className="h-4 w-4 text-emerald-500 shrink-0" />
        ) : (
          <Loader2 className="h-4 w-4 text-[hsl(var(--assistant-accent))] animate-spin shrink-0" />
        )}

        <span className="text-xs sm:text-sm text-[hsl(var(--assistant-text-secondary))] flex-1 truncate">
          {allCompleted
            ? t("assistant.toolCallsCompleted", "{{count}} tool calls completed", { count: toolCalls.length })
            : t("assistant.toolCallsRunning", "Running tools ({{count}})", { count: runningCount || pendingCount || toolCalls.length })
          }
        </span>

        <motion.div
          animate={{ rotate: expanded ? 180 : 0 }}
          transition={{ duration: 0.15 }}
        >
          <ChevronDown className="h-4 w-4 text-[hsl(var(--assistant-text-secondary))]" />
        </motion.div>
      </button>

      {isProcessing && !expanded && (
        <span className="ml-6 text-[11px] text-[hsl(var(--assistant-text-secondary))]">
          {t("assistant.thinking")}
        </span>
      )}

      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
            className="overflow-hidden"
          >
            <div className="ml-5 pl-3 pb-2 border-l border-[hsl(var(--assistant-border-soft))]/80 space-y-1">
              {toolCalls.map((tc, idx) => (
                <ToolCallCard key={tc.id || idx} tc={tc} idx={idx} />
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/** Compact image generation placeholder */
function ImageGeneratingPlaceholder({ prompt }: { prompt?: string }) {
  const { t } = useTranslation();

  return (
    <div className="space-y-2">
      {/* Compact placeholder box */}
      <motion.div
        initial={{ opacity: 0, scale: 0.98 }}
        animate={{ opacity: 1, scale: 1 }}
        className="relative w-[200px] h-[200px] rounded-xl overflow-hidden bg-gradient-to-br from-slate-100 to-slate-200 dark:from-slate-800 dark:to-slate-700"
      >
        {/* Shimmer effect */}
        <motion.div
          className="absolute inset-0 bg-gradient-to-r from-transparent via-white/20 to-transparent dark:via-white/5"
          animate={{ x: ["-100%", "100%"] }}
          transition={{ duration: 1.5, repeat: Infinity, ease: "linear" }}
        />

        {/* Center content */}
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

      {/* Prompt preview */}
      {prompt && (
        <p className="text-[11px] text-slate-400 dark:text-slate-500 truncate max-w-[200px]">
          "{prompt.length > 40 ? prompt.slice(0, 40) + "..." : prompt}"
        </p>
      )}
    </div>
  );
}

export function ChatMessage({ message }: ChatMessageProps) {
  const { t } = useTranslation();
  const isUser = message.role === "user";
  const hasToolCalls = message.toolCalls && message.toolCalls.length > 0;
  const hasSearchStatus = Boolean(message.searchStatus?.length);
  const hasProcessSummaryDetails =
    !!message.processSummary &&
    (
      message.processSummary.status === "failed" ||
      message.processSummary.steps.length > 0 ||
      message.processSummary.tools.length > 0 ||
      !!message.processSummary.contextBudget ||
      !!message.processSummary.contextCompacted
    );
  const hasProcessSummary =
    ASSISTANT_UI_V2 &&
    !!message.processSummary &&
    (message.isStreaming || hasProcessSummaryDetails);
  // Show thinking when streaming but no content yet
  const isThinking = message.isStreaming && !message.content;
  const shouldShowSearchStatus =
    hasSearchStatus &&
    (
      !ASSISTANT_UI_V2 ||
      (!hasProcessSummary && !hasToolCalls && !isThinking && !message.isStreaming)
    );

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.25, 0.1, 0.25, 1] }}
      className={cn(
        "group w-full",
        isUser ? "flex justify-end" : ""
      )}
    >
      {/* User message — warm pill, right-aligned */}
      {isUser ? (
        <div className="max-w-[75%]">
          <AttachmentsDisplay attachments={message.attachments} useV2={ASSISTANT_UI_V2} />
          <div
            className={cn(
              "rounded-3xl px-5 py-3",
              ASSISTANT_UI_V2
                ? "bg-[hsl(var(--assistant-user-bubble))] text-[hsl(var(--assistant-text-primary))]"
                : "bg-primary text-white shadow-sm shadow-primary/15"
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
          {hasProcessSummary && message.processSummary ? (
            <ProcessSummaryBar summary={message.processSummary} />
          ) : hasToolCalls ? (
            <ToolCallsDisplay toolCalls={message.toolCalls} isStreaming={!!message.isStreaming} />
          ) : isThinking && !message.isThinkingStreaming && !message.streamingThinkingContent ? (
            /* Fallback: 3-dot animation for non-thinking models or before thinking_start arrives */
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
          ) : null}

          {/* Thinking Panel — real-time thinking display */}
          {(message.isThinkingStreaming || message.streamingThinkingContent) && (
            <ThinkingPanel
              streamingContent={message.streamingThinkingContent}
              isStreaming={!!message.isThinkingStreaming}
            />
          )}

          {/* Search status display */}
          {shouldShowSearchStatus && message.searchStatus && (
            <SearchStatusDisplay searchStatus={message.searchStatus} />
          )}

          {/* Web search results */}
          {message.webSearchResults && message.webSearchResults.length > 0 && (
            <WebSearchDisplay results={message.webSearchResults} />
          )}

          {/* Context display */}
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

          {/* Thought Process — finalized thinking content (after thinking_end) */}
          {message.thinkingContent && !message.isThinkingStreaming && !message.streamingThinkingContent && (
            <ThinkingPanel
              finalContent={message.thinkingContent}
              isStreaming={false}
            />
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
          ) : !isThinking && !hasToolCalls && !message.isStreaming && (
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
          {!message.isStreaming && message.generatedArtifacts && message.generatedArtifacts.length > 0 && (
            ASSISTANT_UI_V2 ? (
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
                    format={artifact.format === "md" || artifact.format === "markdown" ? "markdown" : "text"}
                    downloadUrl={artifact.url}
                    defaultExpanded={false}
                    maxHeight={300}
                  />
                ))}
              </div>
            )
          )}

          {/* Quiz card */}
          {message.quizData && (
            <div className="mt-4">
              <QuizCard
                quizData={message.quizData}
                existingResult={message.quizResult}
                />
              </div>
            )}

            {/* Stats */}
            {!message.isStreaming && <StatsBadge message={message} />}
          </div>
        )}
    </motion.div>
  );
}
