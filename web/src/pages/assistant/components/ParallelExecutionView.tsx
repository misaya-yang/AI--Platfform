/**
 * Parallel Execution View Component
 *
 * Visualizes parallel tool execution in agentic workflows:
 * - Displays execution groups as steps
 * - Shows "Parallel x{N}" badge when multiple tools execute together
 * - Grid layout for parallel executions (max 3 columns)
 * - Status icons with visual feedback
 * - Progress bars for running tools
 */

import { motion, AnimatePresence } from "framer-motion";
import {
  Clock,
  Loader2,
  CheckCircle2,
  XCircle,
  Zap,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import type {
  ToolExecutionStatus,
  ToolExecution,
  ParallelGroup,
  ParallelExecutionViewProps,
} from "../types";

// =============================================================================
// Status Icon Component
// =============================================================================

function ExecutionStatusIcon({ status }: { status: ToolExecutionStatus }) {
  switch (status) {
    case "pending":
      return <Clock className="h-4 w-4 text-slate-400 dark:text-slate-500" />;
    case "running":
      return <Loader2 className="h-4 w-4 text-blue-500 animate-spin" />;
    case "completed":
      return <CheckCircle2 className="h-4 w-4 text-emerald-500" />;
    case "failed":
      return <XCircle className="h-4 w-4 text-red-500" />;
    default:
      return <Clock className="h-4 w-4 text-slate-400 dark:text-slate-500" />;
  }
}

// =============================================================================
// Progress Bar Component
// =============================================================================

function ExecutionProgressBar({ progress }: { progress: number }) {
  return (
    <div className="w-full h-1.5 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden">
      <motion.div
        className="h-full bg-gradient-to-r from-blue-500 to-cyan-500 rounded-full"
        initial={{ width: 0 }}
        animate={{ width: `${progress}%` }}
        transition={{ duration: 0.3, ease: "easeOut" }}
      />
    </div>
  );
}

// =============================================================================
// Tool Execution Card Component
// =============================================================================

function ToolExecutionCard({ execution }: { execution: ToolExecution }) {
  const getStatusClasses = () => {
    switch (execution.status) {
      case "pending":
        return "bg-slate-50 dark:bg-slate-800/40 border-slate-200 dark:border-slate-700/40";
      case "running":
        return "bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800/40";
      case "completed":
        return "bg-emerald-50 dark:bg-emerald-900/20 border-emerald-200 dark:border-emerald-800/40";
      case "failed":
        return "bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800/40";
      default:
        return "bg-slate-50 dark:bg-slate-800/40 border-slate-200 dark:border-slate-700/40";
    }
  };

  const formatDuration = (ms: number) => {
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  };

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.95 }}
      className={cn(
        "p-3 rounded-xl border transition-all",
        getStatusClasses()
      )}
    >
      {/* Header with tool name and status */}
      <div className="flex items-center gap-2 mb-2">
        <ExecutionStatusIcon status={execution.status} />
        <span className="text-sm font-medium text-slate-700 dark:text-slate-200 truncate flex-1">
          {execution.tool}
        </span>
        {execution.duration !== undefined && execution.status === "completed" && (
          <span className="text-xs text-slate-500 dark:text-slate-400">
            {formatDuration(execution.duration)}
          </span>
        )}
      </div>

      {/* Progress bar for running state */}
      {execution.status === "running" && execution.progress !== undefined && (
        <div className="mt-2">
          <ExecutionProgressBar progress={execution.progress} />
        </div>
      )}

      {/* Result preview for completed */}
      {execution.status === "completed" && execution.result && (
        <p className="mt-2 text-xs text-emerald-600 dark:text-emerald-400 line-clamp-2">
          {execution.result}
        </p>
      )}

      {/* Error message for failed */}
      {execution.status === "failed" && execution.error && (
        <p className="mt-2 text-xs text-red-600 dark:text-red-400 line-clamp-2">
          {execution.error}
        </p>
      )}
    </motion.div>
  );
}

// =============================================================================
// Parallel Group Component
// =============================================================================

function ParallelGroupView({
  group,
  isActive,
  stepNumber,
}: {
  group: ParallelGroup;
  isActive: boolean;
  stepNumber: number;
}) {
  const isParallel = group.executions.length > 1;
  const hasCompleted = group.executions.every(
    (e) => e.status === "completed" || e.status === "failed"
  );
  const hasFailure = group.executions.some((e) => e.status === "failed");

  const getGroupClasses = () => {
    if (hasFailure) {
      return "border-red-200 dark:border-red-800/40 bg-red-50/50 dark:bg-red-900/10";
    }
    if (hasCompleted) {
      return "border-emerald-200 dark:border-emerald-800/40 bg-emerald-50/50 dark:bg-emerald-900/10";
    }
    if (isActive) {
      return "border-blue-200 dark:border-blue-800/40 bg-blue-50/50 dark:bg-blue-900/10";
    }
    return "border-slate-200 dark:border-slate-700/40 bg-slate-50/50 dark:bg-slate-800/20";
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "p-4 rounded-2xl border transition-colors",
        getGroupClasses()
      )}
    >
      {/* Group header */}
      <div className="flex items-center gap-2 mb-3">
        <div
          className={cn(
            "flex items-center justify-center w-6 h-6 rounded-lg text-xs font-medium",
            hasCompleted
              ? "bg-emerald-500/20 text-emerald-600 dark:text-emerald-400"
              : isActive
                ? "bg-blue-500/20 text-blue-600 dark:text-blue-400"
                : "bg-slate-500/20 text-slate-600 dark:text-slate-400"
          )}
        >
          {stepNumber}
        </div>
        <span className="text-xs font-medium text-slate-500 dark:text-slate-400">
          Step {stepNumber}
        </span>
        {isParallel && (
          <Badge
            variant="secondary"
            className="flex items-center gap-1 px-2 py-0.5 bg-violet-100 dark:bg-violet-900/30 text-violet-600 dark:text-violet-400 border-violet-200 dark:border-violet-800/40"
          >
            <Zap className="h-3 w-3" />
            <span>Parallel x{group.executions.length}</span>
          </Badge>
        )}
      </div>

      {/* Execution grid */}
      <div
        className={cn(
          "grid gap-2",
          group.executions.length === 1
            ? "grid-cols-1"
            : group.executions.length === 2
              ? "grid-cols-2"
              : "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3"
        )}
      >
        <AnimatePresence mode="popLayout">
          {group.executions.map((execution) => (
            <ToolExecutionCard key={execution.id} execution={execution} />
          ))}
        </AnimatePresence>
      </div>
    </motion.div>
  );
}

// =============================================================================
// Main ParallelExecutionView Component
// =============================================================================

export function ParallelExecutionView({
  groups,
  currentGroup,
}: ParallelExecutionViewProps) {
  if (groups.length === 0) {
    return null;
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -10 }}
      className="space-y-3"
    >
      <AnimatePresence mode="popLayout">
        {groups.map((group, index) => (
          <ParallelGroupView
            key={group.groupId}
            group={group}
            isActive={group.groupId === currentGroup}
            stepNumber={index + 1}
          />
        ))}
      </AnimatePresence>
    </motion.div>
  );
}

export default ParallelExecutionView;
