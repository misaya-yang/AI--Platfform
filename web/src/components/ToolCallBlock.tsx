/**
 * ToolCallBlock - Refined, stable tool call visualization
 * 
 * Design: Clean, professional with subtle depth
 * - Larger, readable typography
 * - Stable animations without excessive motion
 * - Clear visual hierarchy
 */

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { cn } from "@/lib/utils";
import type { ToolCall } from "@/types/gateway";
import { useTranslation } from "react-i18next";
import { CheckCircle2, Clock, Loader2, AlertCircle, ChevronRight, Terminal, ArrowRight, Sparkles } from "lucide-react";

interface ToolCallBlockProps {
  toolCall: ToolCall;
  result?: string;
  argsText?: string;
  argsValid?: boolean;
}

// Status configurations with refined aesthetics
const statusConfig: Record<string, {
  label: string;
  labelKey: string;
  bg: string;
  border: string;
  text: string;
  icon: typeof CheckCircle2;
  glow: string;
}> = {
  pending: {
    label: "Waiting",
    labelKey: "playground.toolCall.pending",
    bg: "bg-amber-500/8 dark:bg-amber-500/10",
    border: "border-amber-500/25 dark:border-amber-400/20",
    text: "text-amber-600 dark:text-amber-400",
    icon: Clock,
    glow: "shadow-amber-500/5",
  },
  running: {
    label: "Running",
    labelKey: "playground.toolCall.running",
    bg: "bg-sky-500/8 dark:bg-sky-500/12",
    border: "border-sky-500/30 dark:border-sky-400/25",
    text: "text-sky-600 dark:text-sky-400",
    icon: Loader2,
    glow: "shadow-sky-500/10",
  },
  completed: {
    label: "Done",
    labelKey: "playground.toolCall.completed",
    bg: "bg-emerald-500/8 dark:bg-emerald-500/10",
    border: "border-emerald-500/25 dark:border-emerald-400/20",
    text: "text-emerald-600 dark:text-emerald-400",
    icon: CheckCircle2,
    glow: "shadow-emerald-500/5",
  },
  error: {
    label: "Failed",
    labelKey: "playground.toolCall.error",
    bg: "bg-rose-500/8 dark:bg-rose-500/12",
    border: "border-rose-500/30 dark:border-rose-400/25",
    text: "text-rose-600 dark:text-rose-400",
    icon: AlertCircle,
    glow: "shadow-rose-500/10",
  },
};

export function ToolCallBlock({ toolCall, result, argsText, argsValid }: ToolCallBlockProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(false);
  const status = toolCall.status || "pending";
  const config = statusConfig[status] || statusConfig.pending;
  const StatusIcon = config.icon;
  const isRunning = status === "running";

  // Parse arguments
  const rawArgs = argsText ?? toolCall.arguments ?? "";
  const hasValidArgs = argsValid ?? (rawArgs ? (() => {
    try {
      JSON.parse(rawArgs);
      return true;
    } catch {
      return false;
    }
  })() : false);

  let parsedArgs: Record<string, unknown> | null = null;
  if (hasValidArgs && rawArgs) {
    try {
      parsedArgs = JSON.parse(rawArgs);
    } catch {
      parsedArgs = null;
    }
  }

  // Parse result
  let parsedResult: unknown = result;
  try {
    if (result) {
      parsedResult = JSON.parse(result);
    }
  } catch {
    // Keep raw string
  }

  // Generate preview text
  const argsPreview = parsedArgs ? JSON.stringify(parsedArgs) : rawArgs;
  const preview = argsPreview.length > 80 ? `${argsPreview.slice(0, 80)}…` : argsPreview;

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2, ease: [0.25, 0.1, 0.25, 1] }}
      className={cn(
        "relative w-full rounded-xl border overflow-hidden",
        "transition-shadow duration-200",
        config.bg,
        config.border,
        isExpanded ? "shadow-md" : "shadow-sm"
      )}
    >
      {/* Subtle glow effect for running state */}
      {isRunning && (
        <div className="absolute inset-0 pointer-events-none">
          <div className="absolute inset-0 bg-gradient-to-r from-sky-500/0 via-sky-500/5 to-sky-500/0 animate-pulse" />
        </div>
      )}

      {/* Header - Compact */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className={cn(
          "flex w-full items-center gap-2.5 px-3 py-1.5 text-left",
          "transition-colors duration-200",
          "hover:bg-white/5 dark:hover:bg-white/3",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-sky-500/50 focus-visible:ring-inset"
        )}
        type="button"
      >
        {/* Expand chevron */}
        <motion.div
          animate={{ rotate: isExpanded ? 90 : 0 }}
          transition={{ duration: 0.2 }}
          className="flex-shrink-0"
        >
          <ChevronRight className="h-3 w-3 text-slate-400 dark:text-slate-500" />
        </motion.div>

        {/* Tool icon */}
        <div className={cn(
          "flex h-6 w-6 items-center justify-center rounded flex-shrink-0",
          "bg-gradient-to-br from-slate-100 to-slate-50",
          "dark:from-slate-800 dark:to-slate-900",
          "border border-slate-200/50 dark:border-slate-700/50"
        )}>
          <Terminal className={cn("h-3 w-3", config.text)} />
        </div>

        {/* Tool name and preview */}
        <div className="flex-1 min-w-0">
          <span className="font-mono text-[12px] font-semibold text-slate-800 dark:text-slate-100 tracking-tight">
            {toolCall.name || t("playground.toolCall.unknownTool", "unknown_tool")}
          </span>

          {!isExpanded && preview && (
            <p className="text-[10px] text-slate-500 dark:text-slate-400 truncate font-mono">
              {preview}
            </p>
          )}
        </div>

        {/* Status badge - compact */}
        <div className={cn(
          "flex items-center gap-1 px-2 py-0.5 rounded-full flex-shrink-0",
          "text-[10px] font-semibold",
          config.bg,
          config.border,
          config.text,
          "border"
        )}>
          <StatusIcon className={cn(
            "h-2.5 w-2.5",
            isRunning && "animate-spin"
          )} />
          <span>{t(config.labelKey, config.label)}</span>
        </div>
      </button>

      {/* Expandable content */}
      <AnimatePresence initial={false}>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: [0.25, 0.1, 0.25, 1] }}
            className="overflow-hidden"
          >
            <div className="border-t border-slate-200/50 dark:border-slate-700/40">
              {/* Arguments section */}
              {rawArgs && (
                <div className="px-4 py-3 border-b border-slate-200/30 dark:border-slate-700/30">
                  <div className="flex items-center gap-2 mb-2">
                    <ArrowRight className="h-3 w-3 text-slate-400 dark:text-slate-500" />
                    <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                      {hasValidArgs
                        ? t("playground.toolCall.arguments", "Arguments")
                        : t("playground.toolCall.argumentsPartial", "Arguments (streaming...)")}
                    </span>
                    {!hasValidArgs && (
                      <span className="flex h-1.5 w-1.5 ml-1">
                        <span className="animate-ping absolute inline-flex h-1.5 w-1.5 rounded-full bg-sky-400 opacity-75" />
                        <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-sky-500" />
                      </span>
                    )}
                  </div>
                  <pre className={cn(
                    "max-h-40 overflow-auto rounded-lg p-3",
                    "bg-slate-900/90 dark:bg-black/40",
                    "text-[11px] font-mono text-slate-200 leading-relaxed",
                    "border border-slate-700/50",
                    "whitespace-pre-wrap break-words"
                  )}>
                    {parsedArgs ? JSON.stringify(parsedArgs, null, 2) : rawArgs}
                  </pre>
                </div>
              )}

              {/* Result section */}
              {result && (
                <div className="px-4 py-3">
                  <div className="flex items-center gap-2 mb-2">
                    <Sparkles className="h-3 w-3 text-emerald-500" />
                    <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                      {t("playground.toolCall.result", "Result")}
                    </span>
                  </div>
                  <pre className={cn(
                    "max-h-40 overflow-auto rounded-lg p-3",
                    "bg-emerald-950/50 dark:bg-emerald-950/30",
                    "text-[11px] font-mono text-emerald-100 leading-relaxed",
                    "border border-emerald-800/30",
                    "whitespace-pre-wrap break-words"
                  )}>
                    {typeof parsedResult === "object" ? JSON.stringify(parsedResult, null, 2) : String(result)}
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
