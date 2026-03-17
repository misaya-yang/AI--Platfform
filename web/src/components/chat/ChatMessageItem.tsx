import { motion } from "framer-motion";
import { memo, useState, forwardRef } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { StreamOutput } from "@/components/StreamOutput";
import { ToolCallBlock } from "@/components/ToolCallBlock";
import { ThinkingIndicator } from "@/components/ThinkingIndicator";
import type { ChatMessage } from "@/components/ChatWindow";
import { MessageAvatar } from "./MessageAvatar";
import { StatsBadge } from "./StatsBadge";
import { TimelineSection } from "./TimelineSection";
import { ArtifactsSection } from "./ArtifactsSection";

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
        showThinkingIndicator = true,
        index,
      },
      ref
    ) {
      const { t } = useTranslation();
      const isUser = message.role === "user";
      const toolCallsCount = message.toolCalls?.length ?? 0;
      const hasToolCalls = toolCallsCount > 0;
      const canShowToolCalls = showToolCalls && toolCallsMode !== "hidden" && !isUser && hasToolCalls;
      const hasTimeline = showTimeline && !isUser && message.timeline && message.timeline.steps.length > 0;
      const hasArtifacts = !isUser && message.artifacts && message.artifacts.length > 0;
      const assistantDisplayContent = isUser
        ? message.content
        : stripProcessSectionForDisplay(message.content || "");
      const hasVisibleAssistantText = assistantDisplayContent.trim().length > 0;
      const hasRunningToolCalls = message.toolCalls?.some(tc => tc.toolCall.status === "running") ?? false;

      const [isTimelineExpanded, setIsTimelineExpanded] = useState(
        message.timeline?.status === "running"
      );
      const initialToolCallExpand =
        toolCallsMode === "full" || toolCallsDefaultOpen || hasRunningToolCalls;
      const [toolCallsExpanded, setToolCallsExpanded] = useState(
        initialToolCallExpand
      );
      const toolCallsAreForcedOpen = toolCallsMode === "full" || hasRunningToolCalls;
      const shouldShowToolCallsList =
        canShowToolCalls && (toolCallsAreForcedOpen || toolCallsExpanded);
      const shouldShowToolCallsSummary =
        canShowToolCalls &&
        toolCallsMode === "collapsed" &&
        !toolCallsAreForcedOpen &&
        !toolCallsExpanded;

      const contentLikelyFiltered = message.content && message.content.length < 50 && (
        message.content.trim().startsWith("Here is the JSON") ||
        message.content.trim().startsWith("Here's the JSON") ||
        (message.content.trim().startsWith("{") && message.content.trim().endsWith("}")) ||
        message.content.trim() === "```"
      );
      const shouldShowThinking = showThinkingIndicator && !isUser && message.isStreaming &&
        (!message.content || hasRunningToolCalls || contentLikelyFiltered);

      return (
        <motion.div
          ref={ref}
          data-message-role={message.role}
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, ease: "easeOut", delay: index * 0.05 }}
          className={cn(
            "flex w-full gap-4",
            isUser ? "flex-row-reverse" : "flex-row"
          )}
        >
        {/* Avatar */}
        <MessageAvatar isUser={isUser} />

        {/* Content Container */}
        <div className={cn(
          "flex flex-col gap-1.5 min-w-0",
          isUser ? "w-fit max-w-[70%] items-end" : "w-full max-w-[85%] min-w-[280px] sm:min-w-[360px] items-start"
        )}>
          {/* Name Label */}
          <span className={cn(
            "text-xs font-medium px-1",
            isUser ? "text-emerald-600 dark:text-emerald-400" : "text-blue-600 dark:text-blue-400"
          )}>
            {isUser ? t("playground.you", "You") : t("playground.assistant", "AI Assistant")}
          </span>

          {/* Message Bubble */}
          <div
            data-message-surface={isUser ? "user" : "assistant"}
            role={!isUser && message.status === "failed" ? "alert" : undefined}
            aria-live={!isUser && message.status === "failed" ? "assertive" : undefined}
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
                    "space-y-4",
                    message.status === "failed"
                      ? "rounded-2xl rounded-tl-sm border border-rose-500/25 bg-rose-500/8 px-4 py-3"
                      : "px-1 py-1",
                  ]
            )}
          >
            {/* AI Thinking Indicator */}
            {shouldShowThinking && (
              <div
                data-message-supplemental="thinking"
                className="rounded-2xl border border-slate-200/60 bg-white/65 px-4 py-3 shadow-sm shadow-slate-200/30 dark:border-white/8 dark:bg-white/[0.03] dark:shadow-none"
              >
                <ThinkingIndicator />
              </div>
            )}

            {/* AG-UI Timeline Section */}
            {hasTimeline && message.timeline && (
              <div data-message-supplemental="timeline">
                <TimelineSection
                  timeline={message.timeline}
                  isExpanded={isTimelineExpanded}
                  onToggle={() => setIsTimelineExpanded(!isTimelineExpanded)}
                />
              </div>
            )}

            {/* Tool Calls Section (legacy, shown when no timeline) */}
            {shouldShowToolCallsSummary && !hasTimeline && (
              <div data-message-supplemental="tool-summary" className="mb-5 w-full min-w-0">
                <button
                  type="button"
                  onClick={() => setToolCallsExpanded(true)}
                  className={cn(
                    "flex w-full items-center justify-between rounded-xl border",
                  "bg-blue-500/8 dark:bg-blue-500/10 px-4 py-3",
                  "text-[13px] font-medium text-blue-700 dark:text-blue-300",
                  "border-blue-500/20 dark:border-blue-400/20",
                  "transition-colors hover:bg-blue-500/12 dark:hover:bg-blue-500/15"
                )}
              >
                  <span>{t("playground.toolCallsCollapsed", "Tool calls")} ({toolCallsCount})</span>
                  <span className="text-[11px] font-semibold uppercase tracking-wider opacity-70">
                    {t("playground.expand", "Expand")}
                  </span>
                </button>
              </div>
            )}
            {!isUser && shouldShowToolCallsList && !hasTimeline && (
              <div data-message-supplemental="tool-calls" className="mb-5 space-y-3 w-full min-w-0">
                <div className="flex items-center gap-2.5 text-[12px] font-semibold uppercase tracking-wider text-blue-600 dark:text-blue-400">
                  <span className="h-2 w-2 rounded-full bg-gradient-to-br from-blue-500 to-cyan-600" />
                  {t("playground.toolCalls", "Tool Calls")}
                  {toolCallsMode === "collapsed" && (
                    <button
                      type="button"
                      onClick={() => setToolCallsExpanded(false)}
                      className="ml-auto text-[11px] font-semibold uppercase tracking-wider text-blue-500 hover:text-blue-600 dark:text-blue-400 dark:hover:text-blue-300 transition-colors"
                    >
                      {t("playground.collapse", "Collapse")}
                    </button>
                  )}
                </div>
                <div className="space-y-3 w-full">
                  {message.toolCalls?.map((tc, idx) => (
                    <ToolCallBlock
                      key={tc.toolCall.tool_call_id || idx}
                      toolCall={tc.toolCall}
                      result={tc.result}
                      argsText={tc.argsText}
                      argsValid={tc.argsValid}
                    />
                  ))}
                </div>
              </div>
            )}

            {/* Message Content */}
            {(isUser ? message.content : hasVisibleAssistantText) ? (
              <div className={cn(
                "leading-[1.75]",
                isUser
                  ? "text-white text-[15px]"
                  : "max-w-none px-1 text-[15px] text-slate-800 dark:text-zinc-100"
              )}
              data-message-text={isUser ? undefined : "true"}
              >
                {isUser ? (
                  <div className="whitespace-pre-wrap">{message.content}</div>
                ) : (
                    <StreamOutput
                      text={assistantDisplayContent}
                      isStreaming={!!message.isStreaming}
                      id={message.id || `msg-${index}`}
                    />
                  )}
              </div>
            ) : null}

            {/* Artifacts Section */}
            {hasArtifacts && message.artifacts && (
              <div data-message-supplemental="artifacts">
                <ArtifactsSection artifacts={message.artifacts} />
              </div>
            )}
          </div>

          {/* Stats (assistant messages only, after content loaded) */}
          {!isUser && message.stats && !message.isThinking && message.content && (
            <StatsBadge stats={message.stats} />
          )}
        </div>
      </motion.div>
    );
  })
);

ChatMessageItem.displayName = "ChatMessageItem";
