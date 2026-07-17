/**
 * AgentStatusStream - Real-time Agent Status Display Component
 *
 * A Manus-style component that shows real-time Agent execution status with:
 * - Thinking state with breathing animation
 * - RAG retrieval progress
 * - Tool execution visualization
 * - Content generation status
 *
 * Replaces static "research completed" cards with dynamic, transparent feedback.
 */

import * as React from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Brain,
  Search,
  Database,
  Terminal,
  Sparkles,
  ChevronDown,
  Loader2,
  CheckCircle2,
  Code2,
  Globe,
  FileText,
  Wrench,
  Zap,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useTranslation } from "react-i18next";

// ============================================================================
// Types
// ============================================================================

export type AgentStatus =
  | "idle"
  | "thinking"     // TaskPlanner / ReActExecutor thinking
  | "retrieving"   // RAG retrieval
  | "executing"    // Tool execution
  | "generating"   // Content generation
  | "completed"
  | "error";

export interface ToolExecutionInfo {
  id: string;
  name: string;
  displayName?: string;
  args?: Record<string, unknown>;
  status: "pending" | "running" | "completed" | "error";
  result?: string;
  error?: string;
  durationMs?: number;
}

export interface RetrievalInfo {
  query?: string;
  datasetName?: string;
  documentsFound?: number;
  status: "searching" | "completed";
}

export interface AgentStatusStreamState {
  status: AgentStatus;
  phase?: string;
  phaseName?: string;
  phaseProgress?: {
    current: number;
    total: number;
  };
  thinking?: {
    message: string;
    subMessage?: string;
  };
  retrieval?: RetrievalInfo;
  toolExecution?: ToolExecutionInfo;
  generation?: {
    started: boolean;
    tokensGenerated?: number;
  };
}

interface AgentStatusStreamProps {
  state: AgentStatusStreamState;
  className?: string;
  /** Whether the card is collapsible */
  collapsible?: boolean;
  /** Default collapsed state */
  defaultCollapsed?: boolean;
}

// ============================================================================
// Status Config
// ============================================================================

const statusConfig: Record<AgentStatus, {
  icon: React.ElementType;
  color: string;
  bgColor: string;
  borderColor: string;
  glowColor: string;
  label: string;
  animate: boolean;
}> = {
  idle: {
    icon: Brain,
    color: "text-slate-400",
    bgColor: "bg-slate-50 dark:bg-slate-800/50",
    borderColor: "border-slate-200 dark:border-slate-700",
    glowColor: "",
    label: "Ready",
    animate: false,
  },
  thinking: {
    icon: Brain,
    color: "text-primary",
    bgColor: "bg-primary/5",
    borderColor: "border-primary/30 dark:border-primary/30",
    glowColor: "shadow-[0_0_20px_-5px] shadow-primary/30",
    label: "Thinking",
    animate: true,
  },
  retrieving: {
    icon: Search,
    color: "text-primary",
    bgColor: "bg-linear-to-r from-primary/5 via-indigo-500/5 to-blue-500/5",
    borderColor: "border-primary/30 dark:border-primary/30",
    glowColor: "shadow-[0_0_20px_-5px] shadow-primary/30",
    label: "Retrieving",
    animate: true,
  },
  executing: {
    icon: Terminal,
    color: "text-amber-500",
    bgColor: "bg-linear-to-r from-amber-500/5 via-orange-500/5 to-yellow-500/5",
    borderColor: "border-amber-500/30 dark:border-amber-400/30",
    glowColor: "shadow-[0_0_20px_-5px] shadow-amber-500/30",
    label: "Executing",
    animate: true,
  },
  generating: {
    icon: Sparkles,
    color: "text-emerald-500",
    bgColor: "bg-linear-to-r from-emerald-500/5 via-teal-500/5 to-cyan-500/5",
    borderColor: "border-emerald-500/30 dark:border-emerald-400/30",
    glowColor: "shadow-[0_0_20px_-5px] shadow-emerald-500/30",
    label: "Generating",
    animate: true,
  },
  completed: {
    icon: CheckCircle2,
    color: "text-green-500",
    bgColor: "bg-green-50 dark:bg-green-900/20",
    borderColor: "border-green-200 dark:border-green-800",
    glowColor: "",
    label: "Completed",
    animate: false,
  },
  error: {
    icon: Zap,
    color: "text-red-500",
    bgColor: "bg-red-50 dark:bg-red-900/20",
    borderColor: "border-red-200 dark:border-red-800",
    glowColor: "",
    label: "Error",
    animate: false,
  },
};

// ============================================================================
// Sub-components
// ============================================================================

/** Animated status icon with breathing/pulse effect */
function StatusIcon({
  status,
  className,
}: {
  status: AgentStatus;
  className?: string;
}) {
  const config = statusConfig[status];
  const Icon = config.icon;

  return (
    <motion.div
      className={cn(
        "flex items-center justify-center h-10 w-10 rounded-xl transition-all duration-300",
        config.animate
          ? `bg-linear-to-br from-${config.color.replace("text-", "")}/20 to-${config.color.replace("text-", "")}/10`
          : "bg-slate-100 dark:bg-slate-800",
        className
      )}
      animate={config.animate ? {
        scale: [1, 1.05, 1],
        opacity: [0.8, 1, 0.8],
      } : {}}
      transition={{
        duration: 2,
        repeat: Infinity,
        ease: "easeInOut",
      }}
    >
      <Icon className={cn("h-5 w-5", config.color, config.animate && "animate-pulse")} />
    </motion.div>
  );
}

/** Thinking state content */
function ThinkingContent({ state }: { state: AgentStatusStreamState }) {
  const { t } = useTranslation();

  return (
    <div className="flex-1 min-w-0">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        className="space-y-1"
      >
        <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
          {state.thinking?.message || t("agent.status.thinking", "AI Assistant is thinking and planning...")}
        </p>
        {state.thinking?.subMessage && (
          <p className="text-xs text-slate-500 dark:text-slate-400">
            {state.thinking.subMessage}
          </p>
        )}
        {state.phaseProgress && (
          <div className="flex items-center gap-2 mt-2">
            <div className="flex-1 h-1 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden">
              <motion.div
                className="h-full bg-primary rounded-full"
                initial={{ width: 0 }}
                animate={{ width: `${(state.phaseProgress.current / state.phaseProgress.total) * 100}%` }}
                transition={{ duration: 0.3 }}
              />
            </div>
            <span className="text-[10px] text-slate-400">
              {state.phaseProgress.current}/{state.phaseProgress.total}
            </span>
          </div>
        )}
      </motion.div>
    </div>
  );
}

/** Retrieval state content */
function RetrievalContent({ state }: { state: AgentStatusStreamState }) {
  const { t } = useTranslation();
  const { retrieval } = state;

  return (
    <div className="flex-1 min-w-0 space-y-2">
      <div className="flex items-center gap-2">
        <Database className="h-4 w-4 text-primary" />
        <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
          {t("agent.status.retrieving", "Searching knowledge base (RAG)...")}
        </p>
      </div>

      {retrieval?.query && (
        <div className="px-3 py-2 bg-primary/5 rounded-lg border border-primary/10">
          <p className="text-xs text-slate-500 dark:text-slate-400 mb-1">
            {t("agent.status.searchQuery", "Search query:")}
          </p>
          <p className="text-sm text-slate-700 dark:text-slate-300 truncate">
            "{retrieval.query}"
          </p>
        </div>
      )}

      {retrieval?.datasetName && (
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <span className="px-2 py-0.5 bg-slate-100 dark:bg-slate-800 rounded">
            {retrieval.datasetName}
          </span>
          {retrieval.documentsFound !== undefined && (
            <span className="flex items-center gap-1">
              <FileText className="h-3 w-3" />
              {retrieval.documentsFound} {t("agent.status.documents", "documents")}
            </span>
          )}
        </div>
      )}

      {retrieval?.status === "searching" && (
        <div className="flex items-center gap-2">
          <div className="flex gap-1">
            {[0, 1, 2].map((i) => (
              <motion.div
                key={i}
                className="w-1.5 h-1.5 rounded-full bg-primary"
                animate={{
                  scale: [1, 1.5, 1],
                  opacity: [0.5, 1, 0.5],
                }}
                transition={{
                  duration: 0.8,
                  repeat: Infinity,
                  delay: i * 0.2,
                }}
              />
            ))}
          </div>
          <span className="text-xs text-slate-400">
            {t("agent.status.searching", "Searching...")}
          </span>
        </div>
      )}
    </div>
  );
}

/** Tool execution state content */
function ExecutionContent({ state }: { state: AgentStatusStreamState }) {
  const { t } = useTranslation();
  const { toolExecution } = state;

  if (!toolExecution) return null;
  const toolName = toolExecution.name.toLowerCase();
  const toolIcon =
    toolName.includes("code") || toolName.includes("execute") ? (
      <Code2 className="h-4 w-4 text-amber-500" />
    ) : toolName.includes("web") || toolName.includes("search") ? (
      <Globe className="h-4 w-4 text-amber-500" />
    ) : toolName.includes("file") || toolName.includes("document") ? (
      <FileText className="h-4 w-4 text-amber-500" />
    ) : (
      <Wrench className="h-4 w-4 text-amber-500" />
    );

  return (
    <div className="flex-1 min-w-0 space-y-2">
      <div className="flex items-center gap-2">
        {toolIcon}
        <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
          {toolExecution.status === "running"
            ? t("agent.status.executingTool", "Executing tool: {{name}}", { name: toolExecution.displayName || toolExecution.name })
            : t("agent.status.toolComplete", "Tool completed: {{name}}", { name: toolExecution.displayName || toolExecution.name })}
        </p>
      </div>

      {/* Tool arguments (collapsible) */}
      {toolExecution.args && Object.keys(toolExecution.args).length > 0 && (
        <details className="group">
          <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-700 dark:hover:text-slate-300 flex items-center gap-1">
            <ChevronDown className="h-3 w-3 transition-transform group-open:rotate-180" />
            {t("agent.status.viewArgs", "View parameters")}
          </summary>
          <pre className="mt-2 p-2 text-[10px] bg-slate-100 dark:bg-slate-800 rounded-lg overflow-x-auto">
            {JSON.stringify(toolExecution.args, null, 2)}
          </pre>
        </details>
      )}

      {/* Status indicator */}
      <div className="flex items-center gap-2">
        {toolExecution.status === "running" && (
          <Loader2 className="h-3 w-3 text-amber-500 animate-spin" />
        )}
        {toolExecution.status === "completed" && (
          <CheckCircle2 className="h-3 w-3 text-green-500" />
        )}
        <span className={cn(
          "text-xs",
          toolExecution.status === "running" ? "text-amber-600" : "text-green-600"
        )}>
          {toolExecution.status === "running"
            ? t("agent.status.running", "Running...")
            : toolExecution.durationMs
              ? t("agent.status.completedIn", "Completed in {{ms}}ms", { ms: toolExecution.durationMs })
              : t("agent.status.completed", "Completed")}
        </span>
      </div>
    </div>
  );
}

/** Generation state content */
function GenerationContent({ state }: { state: AgentStatusStreamState }) {
  const { t } = useTranslation();

  return (
    <div className="flex-1 min-w-0">
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 text-emerald-500" />
        <p className="text-sm font-medium text-slate-700 dark:text-slate-200">
          {t("agent.status.generating", "Generating response...")}
        </p>
      </div>

      {state.generation?.tokensGenerated !== undefined && (
        <div className="mt-2 flex items-center gap-2 text-xs text-slate-500">
          <motion.span
            key={state.generation.tokensGenerated}
            initial={{ scale: 1.2, color: "#10b981" }}
            animate={{ scale: 1, color: "hsl(var(--muted-foreground))" }}
            className="font-mono"
          >
            {state.generation.tokensGenerated}
          </motion.span>
          <span>{t("agent.status.tokens", "tokens generated")}</span>
        </div>
      )}

      {/* Animated gradient line */}
      <div className="mt-2 h-1 rounded-full overflow-hidden bg-slate-200 dark:bg-slate-700">
        <motion.div
          className="h-full bg-linear-to-r from-emerald-500 via-teal-500 to-cyan-500"
          animate={{
            x: ["-100%", "100%"],
          }}
          transition={{
            duration: 1.5,
            repeat: Infinity,
            ease: "linear",
          }}
          style={{ width: "50%" }}
        />
      </div>
    </div>
  );
}

// ============================================================================
// Main Component
// ============================================================================

export function AgentStatusStream({
  state,
  className,
  collapsible = true,
  defaultCollapsed = false,
}: AgentStatusStreamProps) {
  const { t } = useTranslation();
  const [isCollapsed, setIsCollapsed] = React.useState(defaultCollapsed);
  const config = statusConfig[state.status];

  // Auto-expand when status changes to active states
  React.useEffect(() => {
    if (["thinking", "retrieving", "executing"].includes(state.status)) {
      setIsCollapsed(false);
    }
  }, [state.status]);

  if (state.status === "idle") return null;

  return (
    <motion.div
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -10 }}
      className={cn(
        "rounded-xl border p-4 transition-all duration-300",
        "backdrop-blur-xs",
        config.bgColor,
        config.borderColor,
        config.animate && config.glowColor,
        className
      )}
    >
      {/* Header */}
      <div
        className={cn(
          "flex items-center gap-3",
          collapsible && "cursor-pointer"
        )}
        onClick={() => collapsible && setIsCollapsed(!isCollapsed)}
      >
        <StatusIcon status={state.status} />

        {/* Collapsed view - single line */}
        {isCollapsed ? (
          <div className="flex-1 flex items-center justify-between min-w-0">
            <span className="text-sm font-medium text-slate-600 dark:text-slate-300 truncate">
              {state.phaseName || config.label}
            </span>
            {collapsible && (
              <ChevronDown className="h-4 w-4 text-slate-400 shrink-0" />
            )}
          </div>
        ) : (
          /* Expanded view - full content */
          <AnimatePresence mode="wait">
            {state.status === "thinking" && <ThinkingContent state={state} key="thinking" />}
            {state.status === "retrieving" && <RetrievalContent state={state} key="retrieving" />}
            {state.status === "executing" && <ExecutionContent state={state} key="executing" />}
            {state.status === "generating" && <GenerationContent state={state} key="generating" />}
            {state.status === "completed" && (
              <div className="flex-1">
                <p className="text-sm font-medium text-green-600 dark:text-green-400">
                  {t("agent.status.completed", "Task completed successfully")}
                </p>
              </div>
            )}
          </AnimatePresence>
        )}

        {/* Collapse toggle (expanded view) */}
        {collapsible && !isCollapsed && (
          <motion.div
            animate={{ rotate: 180 }}
            className="shrink-0"
          >
            <ChevronDown className="h-4 w-4 text-slate-400" />
          </motion.div>
        )}
      </div>
    </motion.div>
  );
}

export default AgentStatusStream;
