import { motion, AnimatePresence } from "framer-motion";
import { memo, useState, forwardRef } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { StreamOutput } from "@/components/StreamOutput";
import { ToolCallBlock } from "@/components/ToolCallBlock";
import type { ChatMessage } from "@/components/ChatWindow";
import { MessageAvatar } from "./MessageAvatar";
import { StatsBadge } from "./StatsBadge";
import { TimelineSection } from "./TimelineSection";
import { ArtifactsSection } from "./ArtifactsSection";
import { ChevronDown, Wrench } from "lucide-react";

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

      const [isTimelineExpanded, setIsTimelineExpanded] = useState(
        message.timeline?.status === "running"
      );
      const initialToolCallExpand =
        toolCallsMode === "full" ||
        toolCallsDefaultOpen ||
        hasRunningToolCalls;
      const [toolCallsExpanded, setToolCallsExpanded] =
        useState(initialToolCallExpand);
      const toolCallsAreForcedOpen =
        toolCallsMode === "full" || hasRunningToolCalls;
      const shouldShowToolCallsList =
        canShowToolCalls &&
        (toolCallsAreForcedOpen || toolCallsExpanded);
      const shouldShowToolCallsSummary =
        canShowToolCalls &&
        toolCallsMode === "collapsed" &&
        !toolCallsAreForcedOpen &&
        !toolCallsExpanded;

      // Show a minimal waiting cursor when streaming with no content and no tool calls yet
      const isWaitingForFirstToken =
        !isUser &&
        message.isStreaming &&
        !message.content &&
        !hasToolCalls;

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
              {/* Minimal waiting cursor (Claude Desktop style) */}
              {isWaitingForFirstToken && (
                <div className="px-1 py-2">
                  <span
                    className="inline-block w-[2px] h-5 rounded-full bg-gradient-to-b from-slate-400 to-slate-500 dark:from-slate-400 dark:to-slate-500"
                    style={{ animation: "cursor-blink 1s ease-in-out infinite" }}
                  />
                  <style>{`@keyframes cursor-blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.15; } }`}</style>
                </div>
              )}

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

              {/* Tool Calls - Collapsed Summary */}
              {shouldShowToolCallsSummary && !hasTimeline && (
                <div
                  data-message-supplemental="tool-summary"
                  className="mb-3 w-full min-w-0"
                >
                  <button
                    type="button"
                    onClick={() => setToolCallsExpanded(true)}
                    className={cn(
                      "flex w-full items-center gap-3 rounded-xl border",
                      "bg-slate-50/80 dark:bg-slate-800/40 px-4 py-2.5",
                      "text-[13px] font-medium text-slate-600 dark:text-slate-300",
                      "border-slate-200/60 dark:border-slate-700/40",
                      "transition-all duration-200",
                      "hover:bg-slate-100/80 dark:hover:bg-slate-800/60",
                      "hover:border-slate-300/60 dark:hover:border-slate-600/50"
                    )}
                  >
                    <div className="flex h-6 w-6 items-center justify-center rounded-md bg-blue-100 dark:bg-blue-900/40">
                      <Wrench className="h-3 w-3 text-blue-600 dark:text-blue-400" />
                    </div>
                    <span className="flex-1 text-left">
                      {t(
                        "playground.toolCallsUsed",
                        "{{count}} tool calls",
                        { count: toolCallsCount }
                      )}
                    </span>
                    {completedToolCalls === toolCallsCount && (
                      <span className="text-[10px] font-semibold text-emerald-600 dark:text-emerald-400 uppercase tracking-wider">
                        {t("playground.allCompleted", "All done")}
                      </span>
                    )}
                    <ChevronDown className="h-3.5 w-3.5 text-slate-400" />
                  </button>
                </div>
              )}

              {/* Tool Calls - Expanded List */}
              {!isUser &&
                shouldShowToolCallsList &&
                !hasTimeline && (
                  <div
                    data-message-supplemental="tool-calls"
                    className="mb-3 w-full min-w-0"
                  >
                    {/* Tool calls header */}
                    <div className="flex items-center gap-2 mb-2 px-0.5">
                      <div className="flex items-center gap-2 flex-1">
                        <div className="flex h-5 w-5 items-center justify-center rounded-md bg-blue-100 dark:bg-blue-900/40">
                          <Wrench className="h-2.5 w-2.5 text-blue-600 dark:text-blue-400" />
                        </div>
                        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                          {t("playground.toolCalls", "Tool Calls")}
                        </span>
                        {hasRunningToolCalls && (
                          <span className="flex items-center gap-1 text-[10px] font-medium text-blue-600 dark:text-blue-400">
                            <span className="relative flex h-1.5 w-1.5">
                              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
                              <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-blue-500" />
                            </span>
                            {t(
                              "playground.toolCallsRunning",
                              "In progress"
                            )}
                          </span>
                        )}
                        {!hasRunningToolCalls &&
                          completedToolCalls === toolCallsCount &&
                          toolCallsCount > 0 && (
                            <span className="text-[10px] font-medium text-emerald-600 dark:text-emerald-400">
                              {completedToolCalls}/{toolCallsCount}
                            </span>
                          )}
                      </div>
                      {toolCallsMode === "collapsed" && (
                        <button
                          type="button"
                          onClick={() =>
                            setToolCallsExpanded(false)
                          }
                          className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 transition-colors"
                        >
                          {t("playground.collapse", "Collapse")}
                        </button>
                      )}
                    </div>

                    {/* Tool call list with subtle connecting line */}
                    <div className="relative space-y-2 w-full">
                      {/* Vertical connecting line */}
                      {toolCallsCount > 1 && (
                        <div className="absolute left-[21px] top-3 bottom-3 w-px bg-slate-200/60 dark:bg-slate-700/40" />
                      )}
                      {message.toolCalls?.map((tc, idx) => (
                        <div
                          key={
                            tc.toolCall.tool_call_id || idx
                          }
                          className="relative"
                        >
                          <ToolCallBlock
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
                        </div>
                      ))}
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
                      isStreaming={!!message.isStreaming}
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
          </div>
        </motion.div>
      );
    }
  )
);

ChatMessageItem.displayName = "ChatMessageItem";
