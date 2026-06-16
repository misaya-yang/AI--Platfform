/**
 * ErrorDisplay - Structured error display component
 *
 * Phase 1 Optimization: Displays errors from the agent loop with
 * severity-based styling and actionable suggestions.
 *
 * Features:
 * - Severity-based color coding (info, warning, error, fatal)
 * - Collapsible error details
 * - Phase information for context
 * - Actionable suggestions when available
 */

import * as React from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  AlertCircle,
  AlertTriangle,
  Info,
  XOctagon,
  ChevronDown,
  Lightbulb,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useTranslation } from "react-i18next";
import type { ErrorSeverity, AgentLoopPhase } from "@/api/assistant";

// ============================================================================
// Types
// ============================================================================

export interface StreamError {
  code: string;
  message: string;
  severity: ErrorSeverity;
  recoverable: boolean;
  phase?: AgentLoopPhase;
  suggestion?: string;
  details?: Record<string, unknown>;
  timestamp: number;
}

interface ErrorDisplayProps {
  errors: StreamError[];
  className?: string;
  showAll?: boolean;
  maxVisible?: number;
}

// ============================================================================
// Helpers
// ============================================================================

const severityConfig: Record<
  ErrorSeverity,
  {
    icon: React.ElementType;
    containerClass: string;
    iconClass: string;
    titleClass: string;
    label: string;
  }
> = {
  info: {
    icon: Info,
    containerClass: "bg-blue-50 dark:bg-blue-900/20 border-blue-200 dark:border-blue-800",
    iconClass: "text-blue-500",
    titleClass: "text-blue-700 dark:text-blue-300",
    label: "Info",
  },
  warning: {
    icon: AlertTriangle,
    containerClass: "bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800",
    iconClass: "text-amber-500",
    titleClass: "text-amber-700 dark:text-amber-300",
    label: "Warning",
  },
  error: {
    icon: AlertCircle,
    containerClass: "bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800",
    iconClass: "text-red-500",
    titleClass: "text-red-700 dark:text-red-300",
    label: "Error",
  },
  fatal: {
    icon: XOctagon,
    containerClass: "bg-red-100 dark:bg-red-900/40 border-red-300 dark:border-red-700",
    iconClass: "text-red-600",
    titleClass: "text-red-800 dark:text-red-200",
    label: "Fatal",
  },
};

const phaseLabels: Record<AgentLoopPhase, string> = {
  memory_loading: "Memory Loading",
  scenario_analysis: "Scenario Analysis",
  task_planning: "Task Planning",
  rag_retrieval: "RAG Retrieval",
  context_building: "Context Building",
  execution: "Tool Execution",
  context_compression: "Context Compression",
  generation_storage: "Response Generation",
};

function formatTimestamp(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString();
}

// ============================================================================
// Sub-components
// ============================================================================

function ErrorItem({ error, defaultExpanded = false }: { error: StreamError; defaultExpanded?: boolean }) {
  const [isExpanded, setIsExpanded] = React.useState(defaultExpanded || error.severity === "fatal");
  const config = severityConfig[error.severity];
  const Icon = config.icon;
  const hasDetails = error.suggestion || error.details || error.phase;

  return (
    <motion.div
      initial={{ opacity: 0, y: -10 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "rounded-lg border p-3",
        config.containerClass
      )}
    >
      {/* Header */}
      <div
        className={cn(
          "flex items-start gap-2",
          hasDetails && "cursor-pointer"
        )}
        onClick={() => hasDetails && setIsExpanded(!isExpanded)}
      >
        <Icon className={cn("h-4 w-4 mt-0.5 shrink-0", config.iconClass)} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between gap-2">
            <span className={cn("text-sm font-medium", config.titleClass)}>
              [{error.code}] {error.message}
            </span>
            {hasDetails && (
              <motion.div
                animate={{ rotate: isExpanded ? 180 : 0 }}
                transition={{ duration: 0.2 }}
              >
                <ChevronDown className="h-3.5 w-3.5 text-slate-400" />
              </motion.div>
            )}
          </div>
          {/* Meta info */}
          <div className="flex items-center gap-2 mt-1 text-xs text-slate-500 dark:text-slate-400">
            <span className={cn(
              "px-1.5 py-0.5 rounded text-[10px] font-medium uppercase",
              error.severity === "fatal" ? "bg-red-200 dark:bg-red-800 text-red-800 dark:text-red-200" :
              error.severity === "error" ? "bg-red-100 dark:bg-red-900 text-red-700 dark:text-red-300" :
              error.severity === "warning" ? "bg-amber-100 dark:bg-amber-900 text-amber-700 dark:text-amber-300" :
              "bg-blue-100 dark:bg-blue-900 text-blue-700 dark:text-blue-300"
            )}>
              {config.label}
            </span>
            {error.recoverable && (
              <span className="text-green-600 dark:text-green-400">Recoverable</span>
            )}
            <span className="text-slate-400">
              {formatTimestamp(error.timestamp)}
            </span>
          </div>
        </div>
      </div>

      {/* Expanded details */}
      <AnimatePresence>
        {isExpanded && hasDetails && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="mt-3 pt-3 border-t border-slate-200 dark:border-slate-700 space-y-2">
              {/* Phase info */}
              {error.phase && (
                <div className="text-xs">
                  <span className="text-slate-500 dark:text-slate-400">Phase: </span>
                  <span className="text-slate-700 dark:text-slate-300">
                    {phaseLabels[error.phase] || error.phase}
                  </span>
                </div>
              )}

              {/* Suggestion */}
              {error.suggestion && (
                <div className="flex items-start gap-2 text-xs bg-white dark:bg-slate-800 rounded p-2">
                  <Lightbulb className="h-3.5 w-3.5 text-amber-500 mt-0.5 shrink-0" />
                  <span className="text-slate-600 dark:text-slate-300">
                    {error.suggestion}
                  </span>
                </div>
              )}

              {/* Details */}
              {error.details && Object.keys(error.details).length > 0 && (
                <details className="text-xs">
                  <summary className="cursor-pointer text-slate-500 hover:text-slate-700 dark:hover:text-slate-300">
                    Technical details
                  </summary>
                  <pre className="mt-2 p-2 bg-slate-100 dark:bg-slate-800 rounded overflow-x-auto text-[10px]">
                    {JSON.stringify(error.details, null, 2)}
                  </pre>
                </details>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

// ============================================================================
// Main Component
// ============================================================================

export function ErrorDisplay({
  errors,
  className,
  showAll = false,
  maxVisible = 3,
}: ErrorDisplayProps) {
  const { t } = useTranslation();
  const [showAllErrors, setShowAllErrors] = React.useState(showAll);

  if (errors.length === 0) {
    return null;
  }

  // Sort by severity (fatal first) and timestamp (newest first)
  const severityOrder: Record<ErrorSeverity, number> = { fatal: 0, error: 1, warning: 2, info: 3 };
  const sortedErrors = [...errors].sort((a, b) => {
    const severityDiff = severityOrder[a.severity] - severityOrder[b.severity];
    if (severityDiff !== 0) return severityDiff;
    return b.timestamp - a.timestamp;
  });

  const visibleErrors = showAllErrors ? sortedErrors : sortedErrors.slice(0, maxVisible);
  const hiddenCount = sortedErrors.length - maxVisible;
  const hasFatal = errors.some((e) => e.severity === "fatal");

  return (
    <div className={cn("space-y-2", className)}>
      {/* Header for multiple errors */}
      {errors.length > 1 && (
        <div className="flex items-center justify-between text-sm">
          <span className={cn(
            "font-medium",
            hasFatal ? "text-red-600 dark:text-red-400" : "text-amber-600 dark:text-amber-400"
          )}>
            {hasFatal
              ? t("agent.errors.fatalTitle", "Fatal Error Occurred")
              : t("agent.errors.title", "{{count}} issues detected", { count: errors.length })}
          </span>
          {!showAllErrors && hiddenCount > 0 && (
            <button
              onClick={() => setShowAllErrors(true)}
              className="text-xs text-blue-500 hover:text-blue-600 dark:hover:text-blue-400"
            >
              {t("agent.errors.showMore", "Show {{count}} more", { count: hiddenCount })}
            </button>
          )}
        </div>
      )}

      {/* Error list */}
      <div className="space-y-2">
        {visibleErrors.map((error, index) => (
          <ErrorItem
            key={`${error.code}-${error.timestamp}-${index}`}
            error={error}
            defaultExpanded={index === 0 && error.severity === "fatal"}
          />
        ))}
      </div>

      {/* Collapse button */}
      {showAllErrors && hiddenCount > 0 && (
        <button
          onClick={() => setShowAllErrors(false)}
          className="text-xs text-slate-500 hover:text-slate-700 dark:hover:text-slate-300"
        >
          {t("agent.errors.showLess", "Show less")}
        </button>
      )}
    </div>
  );
}

export default ErrorDisplay;
