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
import { Bot, User, Clock, MessageSquare, FileText, Image as ImageIcon, Database, Globe, Loader2, CheckCircle2, Zap, Brain, PenTool, Cog, Eye, ListTodo, ChevronRight, ChevronDown, Search } from "lucide-react";
import { cn } from "@/lib/utils";
import { StreamOutput } from "@/components/StreamOutput";
import { WebSearchDisplay } from "./WebSearchDisplay";
import { ContextDisplay } from "./ContextDisplay";
import { CitationDisplay } from "./CitationDisplay";
import { DocumentPreview } from "./DocumentPreview";
import type { ChatMessage as ChatMessageType, SearchStatusItem, AgentPhaseStatus } from "../types";

interface ChatMessageProps {
  message: ChatMessageType;
}

/** Manus-style collapsible task panel */
function SearchStatusDisplay({ searchStatus }: { searchStatus: SearchStatusItem[] }) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(true);

  if (!searchStatus || !Array.isArray(searchStatus) || searchStatus.length === 0) return null;

  // Check if all tasks are completed
  const allCompleted = searchStatus.every(item => item.state === "completed");
  const completedCount = searchStatus.filter(item => item.state === "completed").length;
  const totalCount = searchStatus.length;

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
      actionText,
      isSearching,
      isCompleted,
    };
  };

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
}: {
  attachments: ChatMessageType["attachments"];
}) {
  if (!attachments || attachments.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-2 mb-3">
      {attachments.map((att, idx) => (
        <div
          key={idx}
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-white/20 backdrop-blur-sm text-xs text-white/90 border border-white/20"
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
          color: "bg-emerald-500/10 text-emerald-500 border-emerald-500/20",
          dot: "bg-emerald-500"
        };
      case "running":
        return {
          label: t("assistant.toolStatus.running"),
          color: "bg-blue-500/10 text-blue-500 border-blue-500/20",
          dot: "bg-blue-500 animate-pulse"
        };
      case "error":
        return {
          label: t("assistant.toolStatus.error"),
          color: "bg-red-500/10 text-red-500 border-red-500/20",
          dot: "bg-red-500"
        };
      default:
        return {
          label: t("assistant.toolStatus.pending"),
          color: "bg-slate-500/10 text-slate-500 border-slate-500/20",
          dot: "bg-slate-500"
        };
    }
  };

  const statusConfig = getStatusConfig(tc.status);
  const hasExpandableContent = tc.arguments && Object.keys(tc.arguments).length > 0;

  return (
    <motion.div
      key={tc.id || idx}
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ delay: idx * 0.1 }}
      className="group relative overflow-hidden rounded-xl bg-gradient-to-r from-violet-500/5 via-purple-500/5 to-fuchsia-500/5 border border-violet-500/10 dark:border-violet-400/10 hover:border-violet-500/30 transition-all duration-200"
    >
      {/* Glow effect on hover */}
      <div className="absolute inset-0 bg-gradient-to-r from-violet-500/0 via-purple-500/5 to-fuchsia-500/0 opacity-0 group-hover:opacity-100 transition-opacity duration-300" />

      {/* Clickable header */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="relative w-full flex items-start gap-3 p-3 text-left"
      >
        {/* Tool icon */}
        <div className="flex-shrink-0 flex items-center justify-center h-8 w-8 rounded-lg bg-gradient-to-br from-violet-500/20 to-purple-500/20 border border-violet-500/20">
          <div className="text-violet-500 dark:text-violet-400">
            {getToolIcon(tc.name)}
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 min-w-0">
          {/* Tool name */}
          <div className="flex items-center justify-between gap-2">
            <code className="text-sm font-mono font-medium text-slate-700 dark:text-slate-200">
              {tc.name}
            </code>
            {/* Status badge */}
            <span className={cn(
              "flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium border",
              statusConfig.color
            )}>
              <span className={cn("h-1.5 w-1.5 rounded-full", statusConfig.dot)} />
              {statusConfig.label}
            </span>
          </div>

          {/* Arguments preview (collapsed) */}
          {!isExpanded && tc.arguments && Object.keys(tc.arguments).length > 0 && (
            <pre className="mt-1.5 text-[11px] font-mono text-slate-500 dark:text-slate-400 truncate">
              {JSON.stringify(tc.arguments).length > 60
                ? JSON.stringify(tc.arguments).slice(0, 60) + "..."
                : JSON.stringify(tc.arguments)}
            </pre>
          )}
        </div>

        {/* Expand indicator */}
        <motion.div
          animate={{ rotate: isExpanded ? 90 : 0 }}
          transition={{ duration: 0.2 }}
          className="flex-shrink-0 mt-1"
        >
          <ChevronRight className="h-4 w-4 text-slate-300 dark:text-slate-600 group-hover:text-violet-400 transition-colors" />
        </motion.div>
      </button>

      {/* Expandable content */}
      <AnimatePresence>
        {isExpanded && hasExpandableContent && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="px-3 pb-3 pt-0 ml-11">
              {/* Full arguments */}
              {tc.arguments && Object.keys(tc.arguments).length > 0 && (
                <div className="space-y-2">
                  <div className="text-[10px] font-medium text-slate-400 dark:text-slate-500 uppercase tracking-wider">
                    {t("assistant.toolArguments")}
                  </div>
                  <pre className="text-[11px] font-mono text-slate-600 dark:text-slate-300 bg-slate-100 dark:bg-slate-800/50 rounded-lg p-2 overflow-x-auto whitespace-pre-wrap break-all">
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

/** Premium Tool Calls Display - Manus style */
function ToolCallsDisplay({ toolCalls }: { toolCalls: ChatMessageType["toolCalls"] }) {
  const { t } = useTranslation();

  if (!toolCalls || toolCalls.length === 0) return null;

  return (
    <div className="mb-4 space-y-2">
      {/* Section header */}
      <div className="flex items-center gap-2 text-xs font-medium text-violet-500 dark:text-violet-400">
        <span className="h-1.5 w-1.5 rounded-full bg-violet-500 animate-pulse" />
        {t("assistant.toolCalls")}
      </div>

      {/* Tool call cards */}
      <div className="space-y-2">
        {toolCalls.map((tc, idx) => (
          <ToolCallCard key={tc.id || idx} tc={tc} idx={idx} />
        ))}
      </div>
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

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25, ease: "easeOut" }}
      className={cn(
        "flex w-full gap-3",
        isUser ? "flex-row-reverse" : "flex-row"
      )}
    >
      {/* Avatar */}
      <motion.div
        initial={{ scale: 0.9 }}
        animate={{ scale: 1 }}
        transition={{ delay: 0.05, type: "spring", stiffness: 300, damping: 20 }}
        className={cn(
          "flex h-8 w-8 shrink-0 items-center justify-center rounded-xl transition-transform hover:scale-105",
          isUser
            ? "bg-gradient-to-br from-violet-500 to-purple-600 text-white shadow-sm shadow-violet-500/20"
            : "bg-gradient-to-br from-slate-100 to-slate-200 dark:from-slate-700 dark:to-slate-800 text-slate-600 dark:text-slate-300"
        )}
      >
        {isUser ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
      </motion.div>

      {/* Content */}
      <div
        className={cn(
          "flex flex-col min-w-0",
          isUser
            ? "max-w-[calc(100%-48px)] items-end"
            : "max-w-full lg:max-w-[90%] items-start flex-1"
        )}
      >
        <div
          className={cn(
            "relative text-sm",
            isUser
              ? "bg-gradient-to-br from-violet-500 to-purple-600 text-white rounded-2xl rounded-tr-sm px-4 py-3 shadow-sm shadow-violet-500/15"
              : "w-full"
          )}
        >
          {/* Attachments for user messages */}
          {isUser && <AttachmentsDisplay attachments={message.attachments} />}

          {/* Search status display for assistant (GPT-like "Searching..." indicator) */}
          {!isUser && message.searchStatus && message.searchStatus.length > 0 && (
            <SearchStatusDisplay searchStatus={message.searchStatus} />
          )}

          {/* Web search results for assistant */}
          {!isUser && message.webSearchResults && message.webSearchResults.length > 0 && (
            <WebSearchDisplay results={message.webSearchResults} />
          )}

          {/* Context display for assistant messages */}
          {!isUser && message.contexts && message.contexts.length > 0 && (
            <ContextDisplay contexts={message.contexts} />
          )}

          {/* Tool calls display for assistant messages - Manus style */}
          {!isUser && message.toolCalls && message.toolCalls.length > 0 && (
            <ToolCallsDisplay toolCalls={message.toolCalls} />
          )}

          {/* Message content */}
          {isUser ? (
            <div className="whitespace-pre-wrap leading-relaxed">
              {message.content}
            </div>
          ) : (
            <div className="text-slate-700 dark:text-slate-200">
              {/* GPT-style image generation placeholder */}
              {message.isGeneratingImage ? (
                <ImageGeneratingPlaceholder prompt={message.imageGenerationPrompt} />
              ) : message.isStreaming ? (
                <>
                  {/* Streaming content */}
                  {message.content && (
                    <StreamOutput
                      text={message.content}
                      isStreaming={true}
                      id={`msg-${message.id}`}
                    />
                  )}
                  {/* Manus-style thinking indicator */}
                  {message.agentPhase ? (
                    <AgentPhaseDisplay phase={message.agentPhase} />
                  ) : !message.content && (
                    <div className="flex items-center gap-2 py-1">
                      <div className="flex gap-1">
                        {[0, 1, 2].map((i) => (
                          <motion.div
                            key={i}
                            className="w-1.5 h-1.5 rounded-full bg-slate-400 dark:bg-slate-500"
                            animate={{ opacity: [0.3, 1, 0.3] }}
                            transition={{ duration: 1.2, repeat: Infinity, delay: i * 0.2 }}
                          />
                        ))}
                      </div>
                      <span className="text-sm text-slate-500 dark:text-slate-400">
                        {t("assistant.thinking")}
                      </span>
                    </div>
                  )}
                </>
              ) : message.content ? (
                <StreamOutput
                  text={message.content}
                  isStreaming={false}
                  id={`msg-${message.id}`}
                />
              ) : (
                <span className="text-slate-400 italic">
                  {t("assistant.emptyResponse", "(No response)")}
                </span>
              )}
            </div>
          )}

          {/* Phase 3: Citation display for assistant messages */}
          {!isUser && !message.isStreaming && message.ragCitations && message.ragCitations.length > 0 && (
            <CitationDisplay
              citations={message.ragCitations}
              evaluation={message.ragEvaluation}
            />
          )}

          {/* Generated artifacts (documents, images) - Manus style */}
          {!isUser && !message.isStreaming && message.generatedArtifacts && message.generatedArtifacts.length > 0 && (
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
          )}
        </div>

        {/* Stats for assistant messages */}
        {!isUser && !message.isStreaming && <StatsBadge message={message} />}
      </div>
    </motion.div>
  );
}
