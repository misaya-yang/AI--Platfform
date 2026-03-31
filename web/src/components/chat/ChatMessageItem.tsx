import { motion, AnimatePresence } from "framer-motion";
import { memo, useState, useEffect, useRef, forwardRef } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { StreamOutput } from "@/components/StreamOutput";
import { ToolCallBlock } from "@/components/ToolCallBlock";
import type { ChatMessage } from "@/components/ChatWindow";
import { MessageAvatar } from "./MessageAvatar";
import { StatsBadge } from "./StatsBadge";
import { TimelineSection } from "./TimelineSection";
import { ArtifactsSection } from "./ArtifactsSection";
import { ChevronRight, Sparkles, Copy, Share2, RefreshCw, Check } from "lucide-react";

/**
 * Remove internal "Process (brief)" execution traces from assistant output.
 */
function stripProcessSectionForDisplay(text: string): string {
  if (!text || typeof text !== "string") return text;

  const processBlockRe =
    /(?:^|\n)(?:\s*---\s*\n)?\s*Process\s*\(\s*brief\s*\)\s*:[\s\S]*?Actions\s*&\s*observations\s*:\s*(?:\n+\s*[•\-*]?\s*Action:[^\n]*)+/gi;

  const withoutProcess = text.replace(processBlockRe, "\n");
  const removedProcess = withoutProcess !== text;

  let stripped = withoutProcess;
  if (removedProcess) {
    stripped = stripped.replace(/(?:^|\n)\s*---\s*(?=\n|$)/g, "\n");
  }
  stripped = stripped.replace(/\n{3,}/g, "\n\n").trim();

  return stripped;
}

export interface ChatMessageItemProps {
  message: ChatMessage;
  showToolCalls: boolean;
  toolCallsMode?: "full" | "collapsed" | "hidden";
  toolCallsDefaultOpen?: boolean;
  showTimeline?: boolean;
  showThinkingIndicator?: boolean;
  index: number;
  onShare?: () => void;
  onRegenerate?: () => void;
}

export const ChatMessageItem = memo(
  forwardRef<HTMLDivElement, ChatMessageItemProps>(
    function ChatMessageItem(
      {
        message,
        showToolCalls,
        toolCallsMode = "full",
        toolCallsDefaultOpen = true,
        showTimeline = true,
        index,
        onShare,
        onRegenerate,
      },
      ref
    ) {
      const { t } = useTranslation();
      const isUser = message.role === "user";
      const toolCallsCount = message.toolCalls?.length ?? 0;
      const hasToolCalls = toolCallsCount > 0;
      const canShowToolCalls =
        showToolCalls && toolCallsMode !== "hidden" && !isUser && hasToolCalls;
      const hasTimeline =
        showTimeline &&
        !isUser &&
        message.timeline &&
        message.timeline.steps.length > 0;
      const hasArtifacts =
        !isUser && message.artifacts && message.artifacts.length > 0;
      const assistantDisplayContent = isUser
        ? message.content
        : stripProcessSectionForDisplay(message.content || "");
      const hasVisibleAssistantText =
        assistantDisplayContent.trim().length > 0;
      const hasRunningToolCalls =
        message.toolCalls?.some(
          (tc) => tc.toolCall.status === "running"
        ) ?? false;
      const completedToolCalls =
        message.toolCalls?.filter(
          (tc) => tc.toolCall.status === "completed"
        ).length ?? 0;
      const allToolCallsDone =
        hasToolCalls && completedToolCalls === toolCallsCount && !hasRunningToolCalls;

      // Thinking section: visible when streaming OR has tool calls
      const isStreaming = !!message.isStreaming;
      const showThinkingSection =
        !isUser && (isStreaming || hasToolCalls) && !hasTimeline;

      // Claude Desktop style: expanded while streaming, collapsed when done
      const [thinkingExpanded, setThinkingExpanded] = useState(true);
      const hasAutoCollapsed = useRef(false);

      // Auto-collapse thinking section when response completes or is cancelled
      const isFinalState =
        !isStreaming &&
        (message.status === "completed" ||
          message.status === "cancelled" ||
          message.status === "failed" ||
          message.status === "idle");

      useEffect(() => {
        if (isFinalState && !hasAutoCollapsed.current) {
          hasAutoCollapsed.current = true;
          const timer = setTimeout(() => setThinkingExpanded(false), 400);
          return () => clearTimeout(timer);
        }
      }, [isFinalState]);

      // Thinking summary text for collapsed state
      const thinkingSummary = (() => {
        if (isFinalState && hasToolCalls) {
          return t("playground.thinking.toolsDone", "{{count}} tool calls", {
            count: toolCallsCount,
          });
        }
        if (isStreaming && !hasToolCalls && !message.content) {
          return t("playground.thinking.label", "Thinking...");
        }
        if (hasRunningToolCalls) {
          return t("playground.thinking.usingTools", "Using tools...");
        }
        if (allToolCallsDone) {
          return t("playground.thinking.toolsDone", "{{count}} tool calls", {
            count: toolCallsCount,
          });
        }
        return t("playground.thinking.label", "Thinking...");
      })();

      const [isTimelineExpanded, setIsTimelineExpanded] = useState(
        message.timeline?.status === "running"
      );

      return (
        <motion.div
          ref={ref}
          data-message-role={message.role}
          initial={{ opacity: 0, y: 16 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{
            duration: 0.35,
            ease: "easeOut",
            delay: index * 0.04,
          }}
          className={cn(
            "flex w-full gap-4",
            isUser ? "flex-row-reverse" : "flex-row"
          )}
        >
          {/* Avatar */}
          <MessageAvatar isUser={isUser} />

          {/* Content Container */}
          <div
            className={cn(
              "flex flex-col gap-1.5 min-w-0",
              isUser
                ? "w-fit max-w-[70%] items-end"
                : "w-full max-w-[85%] min-w-[280px] sm:min-w-[360px] items-start"
            )}
          >
            {/* Name Label */}
            <span
              className={cn(
                "text-xs font-medium px-1",
                isUser
                  ? "text-emerald-600 dark:text-emerald-400"
                  : "text-blue-600 dark:text-blue-400"
              )}
            >
              {isUser
                ? t("playground.you", "You")
                : t("playground.assistant", "AI Assistant")}
            </span>

            {/* Message Bubble */}
            <div
              data-message-surface={isUser ? "user" : "assistant"}
              role={
                !isUser && message.status === "failed"
                  ? "alert"
                  : undefined
              }
              aria-live={
                !isUser && message.status === "failed"
                  ? "assertive"
                  : undefined
              }
              className={cn(
                "relative min-w-0 transition-all duration-200",
                isUser
                  ? [
                      "px-4 py-2.5",
                      "bg-gradient-to-br from-emerald-500 via-emerald-500 to-teal-600",
                      "text-white text-[15px] leading-relaxed",
                      "rounded-2xl rounded-tr-sm",
                      "shadow-md shadow-emerald-500/20",
                    ]
                  : [
                      "w-full",
                      "space-y-3",
                      message.status === "failed"
                        ? "rounded-2xl rounded-tl-sm border border-rose-500/25 bg-rose-500/8 px-4 py-3"
                        : "px-1 py-1",
                    ]
              )}
            >
              {/* AG-UI Timeline Section */}
              {hasTimeline && message.timeline && (
                <div data-message-supplemental="timeline">
                  <TimelineSection
                    timeline={message.timeline}
                    isExpanded={isTimelineExpanded}
                    onToggle={() =>
                      setIsTimelineExpanded(!isTimelineExpanded)
                    }
                  />
                </div>
              )}

              {/* Claude Desktop-style Thinking Section */}
              {showThinkingSection && canShowToolCalls && (
                <div data-message-supplemental="thinking" className="w-full">
                  {/* Thinking header - always visible, acts as toggle */}
                  <button
                    type="button"
                    onClick={() => setThinkingExpanded(!thinkingExpanded)}
                    className={cn(
                      "flex items-center gap-2 w-full px-2 py-1.5 rounded-lg text-left",
                      "transition-colors duration-150",
                      "hover:bg-slate-100/60 dark:hover:bg-white/[0.04]",
                      "focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500/30"
                    )}
                  >
                    <motion.div
                      animate={{ rotate: thinkingExpanded ? 90 : 0 }}
                      transition={{ duration: 0.15 }}
                      className="flex-shrink-0"
                    >
                      <ChevronRight className="h-3 w-3 text-slate-400 dark:text-slate-500" />
                    </motion.div>

                    {/* Animated sparkle when still thinking */}
                    <Sparkles
                      className={cn(
                        "h-3.5 w-3.5 flex-shrink-0",
                        isStreaming
                          ? "text-violet-500 animate-pulse"
                          : "text-slate-400 dark:text-slate-500"
                      )}
                    />

                    <span
                      className={cn(
                        "text-[12px] font-medium",
                        isStreaming
                          ? "text-violet-600 dark:text-violet-400"
                          : "text-slate-500 dark:text-slate-400"
                      )}
                    >
                      {thinkingSummary}
                    </span>

                    {/* Shimmer bar when still processing */}
                    {isStreaming && (
                      <div className="flex-1 h-[2px] ml-2 rounded-full bg-violet-100 dark:bg-violet-900/30 overflow-hidden max-w-[80px]">
                        <div className="h-full bg-gradient-to-r from-transparent via-violet-500/50 to-transparent animate-shimmer" />
                      </div>
                    )}
                  </button>

                  {/* Expandable tool calls content */}
                  <AnimatePresence initial={false}>
                    {thinkingExpanded && (
                      <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: "auto", opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        transition={{
                          duration: 0.25,
                          ease: [0.25, 0.1, 0.25, 1],
                        }}
                        className="overflow-hidden"
                      >
                        <div className="pl-5 pt-1.5 space-y-2">
                          {message.toolCalls?.map((tc, idx) => (
                            <ToolCallBlock
                              key={
                                tc.toolCall.tool_call_id || idx
                              }
                              toolCall={tc.toolCall}
                              result={tc.result}
                              argsText={tc.argsText}
                              argsValid={tc.argsValid}
                              stepNumber={
                                toolCallsCount > 1
                                  ? idx + 1
                                  : undefined
                              }
                            />
                          ))}
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              )}

              {/* Waiting-only thinking (no tool calls yet) */}
              {showThinkingSection &&
                !canShowToolCalls &&
                isStreaming &&
                !hasVisibleAssistantText && (
                  <div className="flex items-center gap-2 px-2 py-1.5">
                    <Sparkles className="h-3.5 w-3.5 text-violet-500 animate-pulse flex-shrink-0" />
                    <span className="text-[12px] font-medium text-violet-600 dark:text-violet-400">
                      {t("playground.thinking.label", "Thinking...")}
                    </span>
                    <div className="h-[2px] rounded-full bg-violet-100 dark:bg-violet-900/30 overflow-hidden max-w-[60px]">
                      <div className="h-full bg-gradient-to-r from-transparent via-violet-500/50 to-transparent animate-shimmer" />
                    </div>
                  </div>
                )}

              {/* Message Content */}
              {(isUser
                ? message.content
                : hasVisibleAssistantText) ? (
                <div
                  className={cn(
                    "leading-[1.75]",
                    isUser
                      ? "text-white text-[15px]"
                      : "max-w-none px-1 text-[15px] text-slate-800 dark:text-zinc-100"
                  )}
                  data-message-text={
                    isUser ? undefined : "true"
                  }
                >
                  {isUser ? (
                    <div className="whitespace-pre-wrap">
                      {message.content}
                    </div>
                  ) : (
                    <StreamOutput
                      text={assistantDisplayContent}
                      isStreaming={isStreaming}
                      id={message.id || `msg-${index}`}
                    />
                  )}
                </div>
              ) : null}

              {/* Artifacts Section */}
              {hasArtifacts && message.artifacts && (
                <div data-message-supplemental="artifacts">
                  <ArtifactsSection
                    artifacts={message.artifacts}
                  />
                </div>
              )}
            </div>

            {/* Stats (assistant messages only, after content loaded) */}
            {!isUser &&
              message.stats &&
              !message.isThinking &&
              message.content && (
                <StatsBadge stats={message.stats} />
              )}

            {/* Action buttons (assistant messages, after completion) */}
            {!isUser && !isStreaming && hasVisibleAssistantText && (
              <MessageActions
                content={assistantDisplayContent}
                onShare={onShare}
                onRegenerate={onRegenerate}
              />
            )}
          </div>
        </motion.div>
      );
    }
  )
);

ChatMessageItem.displayName = "ChatMessageItem";

// ---------------------------------------------------------------------------
// Action buttons: Copy, Share, Regenerate
// ---------------------------------------------------------------------------

function MessageActions({
  content,
  onShare,
  onRegenerate,
}: {
  content: string;
  onShare?: () => void;
  onRegenerate?: () => void;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      /* fallback: noop */
    }
  };

  return (
    <div className="flex items-center gap-1 mt-1">
      {/* Copy */}
      <button
        type="button"
        onClick={handleCopy}
        className="p-1.5 rounded-md text-slate-400 hover:text-slate-600 hover:bg-slate-100 dark:hover:text-slate-300 dark:hover:bg-white/[0.06] transition-colors"
        title="Copy"
      >
        {copied ? (
          <Check className="h-3.5 w-3.5 text-emerald-500" />
        ) : (
          <Copy className="h-3.5 w-3.5" />
        )}
      </button>

      {/* Share */}
      {onShare && (
        <button
          type="button"
          onClick={onShare}
          className="p-1.5 rounded-md text-slate-400 hover:text-slate-600 hover:bg-slate-100 dark:hover:text-slate-300 dark:hover:bg-white/[0.06] transition-colors"
          title="Share conversation"
        >
          <Share2 className="h-3.5 w-3.5" />
        </button>
      )}

      {/* Regenerate */}
      {onRegenerate && (
        <button
          type="button"
          onClick={onRegenerate}
          className="p-1.5 rounded-md text-slate-400 hover:text-slate-600 hover:bg-slate-100 dark:hover:text-slate-300 dark:hover:bg-white/[0.06] transition-colors"
          title="Regenerate response"
        >
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}
