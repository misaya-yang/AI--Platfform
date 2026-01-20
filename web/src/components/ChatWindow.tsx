import { motion, AnimatePresence } from "framer-motion";
import { memo, useState } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import { StreamOutput } from "@/components/StreamOutput";
import { ToolCallBlock } from "@/components/ToolCallBlock";
import { AgentTimeline, type TimelineState } from "@/components/agent/AgentTimeline";
import { ArtifactList, type ArtifactData } from "@/components/agent/ArtifactCard";
import type { ToolCall } from "@/types/gateway";
import { Bot, User, Clock, Zap, MessageSquare, ChevronDown, ChevronRight, Activity } from "lucide-react";

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

// Stats badge component for cleaner rendering
function StatsBadge({ stats }: { stats: NonNullable<ChatMessage["stats"]> }) {
  const { t } = useTranslation();

  const hasStats = stats.durationMs != null || stats.firstTokenMs != null || stats.totalTokens != null;
  if (!hasStats) return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.2 }}
      className="flex flex-wrap items-center gap-2 text-[10px] mt-2"
    >
      {/* Duration */}
      {stats.durationMs != null && (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-muted/50 text-muted-foreground">
          <Clock className="h-3 w-3" />
          {(stats.durationMs / 1000).toFixed(2)}s
        </span>
      )}

      {/* TTFT (First Token Time) */}
      {stats.firstTokenMs != null && (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-blue-500/10 text-blue-600 dark:text-blue-400">
          <Zap className="h-3 w-3" />
          {t("playground.stats.ttft", "TTFT")}: {stats.firstTokenMs}ms
        </span>
      )}

      {/* Token count */}
      {stats.totalTokens != null && (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-muted/50 text-muted-foreground group relative">
          <MessageSquare className="h-3 w-3" />
          <span>{stats.totalTokens} {t("playground.stats.tokens", "tokens")}</span>
          {stats.inputTokens != null && stats.outputTokens != null && (
            <span className="text-muted-foreground/50 ml-1">
              ({stats.inputTokens} {t("playground.stats.in", "in")} / {stats.outputTokens} {t("playground.stats.out", "out")})
            </span>
          )}
        </span>
      )}
    </motion.div>
  );
}

/** Collapsible timeline section for Manus-style execution visualization */
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
    <div className="mb-3 w-full">
      {/* Collapsible Header */}
      <button
        type="button"
        onClick={onToggle}
        className={cn(
          "flex items-center gap-2 w-full px-3 py-2 rounded-lg transition-colors",
          "text-left text-xs font-medium",
          "bg-violet-500/5 hover:bg-violet-500/10 border border-violet-500/20",
          isRunning && "border-violet-500/40"
        )}
      >
        <div className={cn(
          "flex items-center justify-center h-5 w-5 rounded-md",
          isRunning
            ? "bg-violet-500/20 text-violet-600 dark:text-violet-400"
            : "bg-violet-500/10 text-violet-500"
        )}>
          <Activity className={cn("h-3.5 w-3.5", isRunning && "animate-pulse")} />
        </div>
        <span className="flex-1 text-muted-foreground">
          {isRunning
            ? t("playground.timeline.running", "Agent working...")
            : t("playground.timeline.completed", "{{count}} steps completed", { count: stepCount })}
        </span>
        {isExpanded ? (
          <ChevronDown className="h-4 w-4 text-muted-foreground" />
        ) : (
          <ChevronRight className="h-4 w-4 text-muted-foreground" />
        )}
      </button>

      {/* Timeline Content */}
      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="pt-2">
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

/** Artifacts section for displaying generated files */
function ArtifactsSection({ artifacts }: { artifacts: ArtifactData[] }) {
  const { t } = useTranslation();

  if (!artifacts || artifacts.length === 0) return null;

  return (
    <div className="mt-3 pt-3 border-t border-border/30">
      <div className="flex items-center gap-2 text-[10px] font-medium uppercase tracking-wide text-muted-foreground mb-2">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-500/60" />
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
    </div>
  );
}

const ChatMessageItem = memo(
  function ChatMessageItem({ message, showToolCalls, showTimeline = true, index }: {
    message: ChatMessage;
    showToolCalls: boolean;
    showTimeline?: boolean;
    index: number;
  }) {
    const { t } = useTranslation();
    const isUser = message.role === "user";
    const hasToolCalls = showToolCalls && !isUser && message.toolCalls && message.toolCalls.length > 0;
    const hasTimeline = showTimeline && !isUser && message.timeline && message.timeline.steps.length > 0;
    const hasArtifacts = !isUser && message.artifacts && message.artifacts.length > 0;

    // Timeline expansion state - auto-expand when running
    const [isTimelineExpanded, setIsTimelineExpanded] = useState(
      message.timeline?.status === "running"
    );

    return (
      <div
        className={cn(
          "flex w-full gap-4 animate-in fade-in-0 slide-in-from-bottom-2 duration-300",
          isUser ? "flex-row-reverse" : "flex-row"
        )}
      >
        {/* Avatar */}
        <div className={cn(
          "flex h-9 w-9 shrink-0 items-center justify-center rounded-xl shadow-md transition-transform hover:scale-105",
          isUser
            ? "bg-gradient-to-br from-emerald-500 to-teal-600 text-white"
            : "bg-gradient-to-br from-violet-500 via-purple-500 to-fuchsia-500 text-white"
        )}>
          {isUser ? <User className="h-5 w-5" /> : <Bot className="h-5 w-5" />}
        </div>

        {/* Content Bubble */}
        <div className={cn(
          "flex flex-col gap-2",
          isUser ? "max-w-[85%] items-end" : "max-w-[85%] min-w-[280px] sm:min-w-[360px] items-start"
        )}>
          {/* Name Label */}
          <span className="text-xs text-muted-foreground ml-1">
            {isUser ? t("playground.you", "You") : t("playground.assistant", "AI Assistant")}
          </span>

          <div
            className={cn(
              "relative px-5 py-3.5 text-sm shadow-lg min-w-0",
              isUser
                ? "bg-gradient-to-br from-emerald-500 to-teal-600 text-white rounded-tl-2xl rounded-tr-sm rounded-br-2xl rounded-bl-2xl shadow-emerald-500/20"
                : "bg-white dark:bg-zinc-900/90 border border-border/30 rounded-tl-sm rounded-tr-2xl rounded-br-2xl rounded-bl-2xl dark:shadow-black/20"
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
            {!isUser && hasToolCalls && !hasTimeline && (
              <div className="mb-3 space-y-2 w-full min-w-0">
                <div className="flex items-center gap-2 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                  <span className="h-1.5 w-1.5 rounded-full bg-purple-500/60" />
                  {t("playground.toolCalls", "Tool Calls")}
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
              <div className={cn("leading-relaxed", isUser ? "text-white" : "text-foreground")}>
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

            {/* AI Thinking / Loading Indicator */}
            {!message.content && !hasToolCalls && !hasTimeline && !isUser && (
              <div className="flex items-center gap-2 h-6">
                {message.isThinking ? (
                  <>
                    <span className="text-sm text-muted-foreground">
                      {t("playground.thinking", "Thinking...")}
                    </span>
                    <div className="flex items-center gap-1">
                      <div className="h-2 w-2 animate-bounce rounded-full bg-violet-500 [animation-delay:-0.3s]" />
                      <div className="h-2 w-2 animate-bounce rounded-full bg-purple-500 [animation-delay:-0.15s]" />
                      <div className="h-2 w-2 animate-bounce rounded-full bg-fuchsia-500" />
                    </div>
                  </>
                ) : (
                  <div className="flex items-center gap-1.5">
                    <div className="h-2 w-2 animate-bounce rounded-full bg-violet-500 [animation-delay:-0.3s]" />
                    <div className="h-2 w-2 animate-bounce rounded-full bg-purple-500 [animation-delay:-0.15s]" />
                    <div className="h-2 w-2 animate-bounce rounded-full bg-fuchsia-500" />
                  </div>
                )}
              </div>
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
      </div>
    );
  },
  (prev, next) =>
    prev.message === next.message &&
    prev.showToolCalls === next.showToolCalls &&
    prev.showTimeline === next.showTimeline &&
    prev.index === next.index
);

export interface ChatWindowProps {
  messages: ChatMessage[];
  showToolCalls?: boolean;
  /** Show AG-UI timeline in assistant messages (default: true) */
  showTimeline?: boolean;
}

export function ChatWindow({
  messages,
  showToolCalls = true,
  showTimeline = true,
}: ChatWindowProps) {
  return (
    <div className="mx-auto w-full max-w-4xl space-y-6 px-4 py-8">
      {messages.map((message, i) => (
        <ChatMessageItem
          key={i}
          message={message}
          showToolCalls={showToolCalls}
          showTimeline={showTimeline}
          index={i}
        />
      ))}
    </div>
  );
}
