import { motion, AnimatePresence } from "framer-motion";
import { memo, useState, forwardRef } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { StreamOutput } from "@/components/StreamOutput";
import { ToolCallBlock } from "@/components/ToolCallBlock";
import { ThinkingIndicator } from "@/components/ThinkingIndicator";
import { AgentTimeline, type TimelineState } from "@/components/agent/AgentTimeline";
import { ArtifactList, type ArtifactData } from "@/components/agent/ArtifactCard";
import type { ToolCall } from "@/types/gateway";
import { Bot, User, Clock, Zap, MessageSquare, ChevronDown, Activity, Sparkles } from "lucide-react";

export interface ToolCallWithResult {
  toolCall: ToolCall;
  result?: string;
  argsText?: string;
  argsValid?: boolean;
}

export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  toolCalls?: ToolCallWithResult[];
  isThinking?: boolean;
  isStreaming?: boolean;
  stats?: {
    durationMs?: number;
    inputTokens?: number;
    outputTokens?: number;
    totalTokens?: number;
    firstTokenMs?: number;
  };
  /** AG-UI Protocol: Agent execution timeline */
  timeline?: TimelineState;
  /** AG-UI Protocol: Generated artifacts (documents, images, etc.) */
  artifacts?: ArtifactData[];
};

// Premium stats badge with glassmorphism
function StatsBadge({ stats }: { stats: NonNullable<ChatMessage["stats"]> }) {
  const { t } = useTranslation();

  const hasStats = stats.durationMs != null || stats.firstTokenMs != null || stats.totalTokens != null;
  if (!hasStats) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.3, duration: 0.4, ease: "easeOut" }}
      className="flex flex-wrap items-center gap-2 text-[10px] mt-3 ml-1"
    >
      {/* Duration - with subtle gradient */}
      {stats.durationMs != null && (
        <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-gradient-to-r from-slate-100 to-slate-50 dark:from-zinc-800/80 dark:to-zinc-800/50 text-slate-600 dark:text-zinc-400 border border-slate-200/50 dark:border-zinc-700/50 backdrop-blur-sm">
          <Clock className="h-3 w-3 text-slate-500 dark:text-zinc-500" />
          <span className="font-medium">{(stats.durationMs / 1000).toFixed(2)}s</span>
        </span>
      )}

      {/* TTFT - accent color */}
      {stats.firstTokenMs != null && (
        <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-gradient-to-r from-blue-500/10 to-indigo-500/10 text-blue-600 dark:text-blue-400 border border-blue-200/50 dark:border-blue-500/20 backdrop-blur-sm">
          <Zap className="h-3 w-3" />
          <span className="font-medium">{t("playground.stats.ttft", "TTFT")}: {stats.firstTokenMs}ms</span>
        </span>
      )}

      {/* Token count - with detail tooltip */}
      {stats.totalTokens != null && (
        <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-gradient-to-r from-slate-100 to-slate-50 dark:from-zinc-800/80 dark:to-zinc-800/50 text-slate-600 dark:text-zinc-400 border border-slate-200/50 dark:border-zinc-700/50 backdrop-blur-sm">
          <MessageSquare className="h-3 w-3 text-slate-500 dark:text-zinc-500" />
          <span className="font-medium">{stats.totalTokens}</span>
          <span className="text-slate-400 dark:text-zinc-500">{t("playground.stats.tokens", "tokens")}</span>
          {stats.inputTokens != null && stats.outputTokens != null && (
            <span className="text-slate-400 dark:text-zinc-500 text-[9px]">
              ({stats.inputTokens}↓ {stats.outputTokens}↑)
            </span>
          )}
        </span>
      )}
    </motion.div>
  );
}

/** Collapsible timeline section - Manus-inspired design */
function TimelineSection({
  timeline,
  isExpanded,
  onToggle,
}: {
  timeline: TimelineState;
  isExpanded: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const stepCount = timeline.steps.length;
  const isRunning = timeline.status === "running";

  if (stepCount === 0) return null;

  return (
    <div className="mb-4 w-full">
      {/* Collapsible Header - glassmorphism style */}
      <button
        type="button"
        onClick={onToggle}
        className={cn(
          "flex items-center gap-3 w-full px-4 py-2.5 rounded-xl transition-all duration-200",
          "text-left text-xs font-medium",
          "bg-gradient-to-r from-violet-500/5 via-purple-500/5 to-fuchsia-500/5",
          "hover:from-violet-500/10 hover:via-purple-500/10 hover:to-fuchsia-500/10",
          "border border-violet-500/20 dark:border-violet-400/20",
          "backdrop-blur-sm",
          isRunning && "border-violet-500/40 shadow-[0_0_15px_-3px] shadow-violet-500/20"
        )}
      >
        <div className={cn(
          "flex items-center justify-center h-6 w-6 rounded-lg transition-all duration-200",
          isRunning
            ? "bg-gradient-to-br from-violet-500 to-purple-600 text-white shadow-lg shadow-violet-500/30"
            : "bg-violet-500/10 text-violet-600 dark:text-violet-400"
        )}>
          <Activity className={cn("h-3.5 w-3.5", isRunning && "animate-pulse")} />
        </div>
        <span className="flex-1 text-slate-600 dark:text-zinc-300">
          {isRunning
            ? t("playground.timeline.running", "Agent working...")
            : t("playground.timeline.completed", "{{count}} steps completed", { count: stepCount })}
        </span>
        <motion.div
          animate={{ rotate: isExpanded ? 180 : 0 }}
          transition={{ duration: 0.2 }}
        >
          <ChevronDown className="h-4 w-4 text-violet-500/60" />
        </motion.div>
      </button>

      {/* Timeline Content */}
      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: "easeInOut" }}
            className="overflow-hidden"
          >
            <div className="pt-3 pl-2">
              <AgentTimeline
                state={timeline}
                compact
              />
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/** Artifacts section with premium card design */
function ArtifactsSection({ artifacts }: { artifacts: ArtifactData[] }) {
  const { t } = useTranslation();

  if (!artifacts || artifacts.length === 0) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.2 }}
      className="mt-4 pt-4 border-t border-slate-200/50 dark:border-zinc-700/50"
    >
      <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wider text-emerald-600 dark:text-emerald-400 mb-3">
        <Sparkles className="h-3 w-3" />
        {t("playground.artifacts", "Generated Files")}
      </div>
      <ArtifactList
        artifacts={artifacts}
        variant="compact"
        onArtifactClick={(artifact) => {
          if (artifact.url) {
            window.open(artifact.url, "_blank");
          }
        }}
      />
    </motion.div>
  );
}

/** Premium Avatar Component */
function Avatar({ isUser }: { isUser: boolean }) {
  return (
    <motion.div
      initial={{ scale: 0.8, opacity: 0 }}
      animate={{ scale: 1, opacity: 1 }}
      transition={{ duration: 0.3, ease: "easeOut" }}
      className="relative"
    >
      {/* Glow effect */}
      <div className={cn(
        "absolute inset-0 rounded-xl blur-lg opacity-40",
        isUser
          ? "bg-gradient-to-br from-emerald-400 to-teal-500"
          : "bg-gradient-to-br from-violet-500 via-purple-500 to-fuchsia-500"
      )} />

      {/* Avatar */}
      <div className={cn(
        "relative flex h-10 w-10 shrink-0 items-center justify-center rounded-xl",
        "shadow-lg transition-transform duration-200 hover:scale-105",
        isUser
          ? "bg-gradient-to-br from-emerald-500 via-emerald-500 to-teal-600 text-white shadow-emerald-500/25"
          : "bg-gradient-to-br from-violet-500 via-purple-500 to-fuchsia-600 text-white shadow-violet-500/25"
      )}>
        {isUser ? (
          <User className="h-5 w-5" strokeWidth={2.5} />
        ) : (
          <Bot className="h-5 w-5" strokeWidth={2.5} />
        )}
      </div>
    </motion.div>
  );
}

interface ChatMessageItemProps {
  message: ChatMessage;
  showToolCalls: boolean;
  toolCallsMode?: "full" | "collapsed" | "hidden";
  toolCallsDefaultOpen?: boolean;
  showTimeline?: boolean;
  showThinkingIndicator?: boolean;
  index: number;
}

const ChatMessageItem = memo(
  forwardRef<HTMLDivElement, ChatMessageItemProps>(
    function ChatMessageItem(
      {
        message,
        showToolCalls,
        toolCallsMode = "full",
        toolCallsDefaultOpen = false,
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

      // Timeline expansion state - auto-expand when running
      const [isTimelineExpanded, setIsTimelineExpanded] = useState(
        message.timeline?.status === "running"
      );
      const [toolCallsExpanded, setToolCallsExpanded] = useState(toolCallsDefaultOpen);
      const shouldShowToolCallsList = canShowToolCalls && (toolCallsMode === "full" || toolCallsExpanded);
      const shouldShowToolCallsSummary = canShowToolCalls && toolCallsMode === "collapsed" && !toolCallsExpanded;

      return (
        <motion.div
          ref={ref}
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.4, ease: "easeOut", delay: index * 0.05 }}
          className={cn(
            "flex w-full gap-4",
            isUser ? "flex-row-reverse" : "flex-row"
          )}
        >
        {/* Avatar */}
        <Avatar isUser={isUser} />

        {/* Content Container */}
        <div className={cn(
          "flex flex-col gap-1.5",
          isUser ? "max-w-[85%] items-end" : "max-w-[85%] min-w-[300px] sm:min-w-[400px] items-start"
        )}>
          {/* Name Label */}
          <span className={cn(
            "text-xs font-medium px-1",
            isUser ? "text-emerald-600 dark:text-emerald-400" : "text-violet-600 dark:text-violet-400"
          )}>
            {isUser ? t("playground.you", "You") : t("playground.assistant", "AI Assistant")}
          </span>

          {/* Message Bubble */}
          <div
            className={cn(
              "relative text-sm min-w-0 transition-all duration-200",
              isUser
                ? [
                    // User message - modern gradient with glass effect
                    "px-5 py-3.5",
                    "bg-gradient-to-br from-emerald-500 via-emerald-500 to-teal-600",
                    "text-white",
                    "rounded-2xl rounded-tr-md",
                    "shadow-lg shadow-emerald-500/20",
                  ]
                : [
                    // Assistant message - clean, minimal design
                    "px-5 py-4",
                    "bg-white dark:bg-zinc-900/90",
                    "text-slate-700 dark:text-zinc-200",
                    "rounded-2xl rounded-tl-md",
                    "border border-slate-200/80 dark:border-zinc-800",
                    "shadow-xl shadow-slate-200/50 dark:shadow-black/20",
                  ]
            )}
          >
            {/* AG-UI Timeline Section */}
            {hasTimeline && message.timeline && (
              <TimelineSection
                timeline={message.timeline}
                isExpanded={isTimelineExpanded}
                onToggle={() => setIsTimelineExpanded(!isTimelineExpanded)}
              />
            )}

            {/* Tool Calls Section (legacy, shown when no timeline) */}
            {shouldShowToolCallsSummary && !hasTimeline && (
              <div className="mb-4 w-full min-w-0">
                <button
                  type="button"
                  onClick={() => setToolCallsExpanded(true)}
                  className={cn(
                    "flex w-full items-center justify-between rounded-lg border border-purple-200/60",
                    "bg-purple-50/60 px-3 py-2 text-xs font-medium text-purple-700",
                    "transition-colors hover:bg-purple-100/70 dark:border-purple-900/60",
                    "dark:bg-purple-950/40 dark:text-purple-200 dark:hover:bg-purple-900/30"
                  )}
                >
                  <span>{t("playground.toolCallsCollapsed", "Tool calls")} ({toolCallsCount})</span>
                  <span className="text-[10px] uppercase tracking-wide">
                    {t("playground.expand", "Expand")}
                  </span>
                </button>
              </div>
            )}
            {!isUser && shouldShowToolCallsList && !hasTimeline && (
              <div className="mb-4 space-y-2 w-full min-w-0">
                <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wider text-purple-600 dark:text-purple-400">
                  <span className="h-1.5 w-1.5 rounded-full bg-purple-500" />
                  {t("playground.toolCalls", "Tool Calls")}
                  {toolCallsMode === "collapsed" && (
                    <button
                      type="button"
                      onClick={() => setToolCallsExpanded(false)}
                      className="ml-auto text-[10px] font-medium uppercase tracking-wide text-purple-500 hover:text-purple-600 dark:text-purple-300 dark:hover:text-purple-200"
                    >
                      {t("playground.collapse", "Collapse")}
                    </button>
                  )}
                </div>
                <div className="space-y-2 w-full">
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
            {message.content ? (
              <div className={cn(
                "leading-relaxed",
                isUser ? "text-white" : "text-slate-700 dark:text-zinc-200"
              )}>
                {isUser ? (
                  <div className="whitespace-pre-wrap">{message.content}</div>
                ) : (
                  <StreamOutput
                    text={message.content}
                    isStreaming={!!message.isStreaming}
                    id={`msg-${index}`}
                  />
                )}
              </div>
            ) : null}

            {/* AI Thinking / Loading Indicator - Manus-style */}
            {showThinkingIndicator && !message.content && !canShowToolCalls && !hasTimeline && !isUser && (
              <ThinkingIndicator />
            )}

            {/* Artifacts Section */}
            {hasArtifacts && message.artifacts && (
              <ArtifactsSection artifacts={message.artifacts} />
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

// Custom memo comparison to prevent unnecessary re-renders
ChatMessageItem.displayName = "ChatMessageItem";

export interface ChatWindowProps {
  messages: ChatMessage[];
  showToolCalls?: boolean;
  toolCallsMode?: "full" | "collapsed" | "hidden";
  toolCallsDefaultOpen?: boolean;
  /** Show AG-UI timeline in assistant messages (default: true) */
  showTimeline?: boolean;
  showThinkingIndicator?: boolean;
}

export function ChatWindow({
  messages,
  showToolCalls = true,
  toolCallsMode = "full",
  toolCallsDefaultOpen = false,
  showTimeline = true,
  showThinkingIndicator = true,
}: ChatWindowProps) {
  return (
    <div className="mx-auto w-full max-w-4xl space-y-8 px-4 py-10">
      <AnimatePresence mode="popLayout">
        {messages.map((message, i) => (
          <ChatMessageItem
            key={`${message.role}-${i}`}
            message={message}
            showToolCalls={showToolCalls}
            toolCallsMode={toolCallsMode}
            toolCallsDefaultOpen={toolCallsDefaultOpen}
            showTimeline={showTimeline}
            showThinkingIndicator={showThinkingIndicator}
            index={i}
          />
        ))}
      </AnimatePresence>
    </div>
  );
}
