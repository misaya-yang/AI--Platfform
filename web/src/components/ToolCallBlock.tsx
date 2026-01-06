import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { cn } from "@/lib/utils";
import type { ToolCall } from "@/types/gateway";
import { useTranslation } from "react-i18next";

interface ToolCallBlockProps {
  toolCall: ToolCall;
  result?: string;
  argsText?: string;
  argsValid?: boolean;
}

// Status configurations with refined color palette
const statusConfig: Record<string, {
  label: string;
  labelKey: string;
  bgGradient: string;
  borderColor: string;
  textColor: string;
  iconBg: string;
  pulseColor: string;
}> = {
  pending: {
    label: "Pending",
    labelKey: "playground.toolCall.pending",
    bgGradient: "from-amber-500/5 via-amber-400/5 to-orange-500/5",
    borderColor: "border-amber-500/30",
    textColor: "text-amber-600 dark:text-amber-400",
    iconBg: "bg-amber-500/15",
    pulseColor: "bg-amber-400",
  },
  running: {
    label: "Running",
    labelKey: "playground.toolCall.running",
    bgGradient: "from-blue-500/8 via-cyan-400/8 to-blue-600/8",
    borderColor: "border-blue-500/40",
    textColor: "text-blue-600 dark:text-blue-400",
    iconBg: "bg-blue-500/20",
    pulseColor: "bg-blue-400",
  },
  completed: {
    label: "Completed",
    labelKey: "playground.toolCall.completed",
    bgGradient: "from-emerald-500/5 via-green-400/5 to-emerald-600/5",
    borderColor: "border-emerald-500/30",
    textColor: "text-emerald-600 dark:text-emerald-400",
    iconBg: "bg-emerald-500/15",
    pulseColor: "bg-emerald-400",
  },
  error: {
    label: "Error",
    labelKey: "playground.toolCall.error",
    bgGradient: "from-red-500/8 via-rose-400/8 to-red-600/8",
    borderColor: "border-red-500/40",
    textColor: "text-red-600 dark:text-red-400",
    iconBg: "bg-red-500/20",
    pulseColor: "bg-red-400",
  },
};

// Tool icon component with gear animation for running state
function ToolIcon({ status }: { status: string }) {
  const isRunning = status === "running";

  return (
    <div className="relative">
      <svg
        className={cn(
          "h-4 w-4 transition-transform duration-500",
          isRunning && "animate-[spin_3s_linear_infinite]"
        )}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M11.42 15.17L17.25 21A2.652 2.652 0 0021 17.25l-5.877-5.877M11.42 15.17l2.496-3.03c.317-.384.74-.626 1.208-.766M11.42 15.17l-4.655 5.653a2.548 2.548 0 11-3.586-3.586l6.837-5.63m5.108-.233c.55-.164 1.163-.188 1.743-.14a4.5 4.5 0 004.486-6.336l-3.276 3.277a3.004 3.004 0 01-2.25-2.25l3.276-3.276a4.5 4.5 0 00-6.336 4.486c.091 1.076-.071 2.264-.904 2.95l-.102.085m-1.745 1.437L5.909 7.5H4.5L2.25 3.75l1.5-1.5L7.5 4.5v1.409l4.26 4.26m-1.745 1.437l1.745-1.437m6.615 8.206L15.75 15.75M4.867 19.125h.008v.008h-.008v-.008z"
        />
      </svg>
    </div>
  );
}

// Status indicator with animated effects
function StatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  const config = statusConfig[status] || statusConfig.pending;
  const isRunning = status === "running";

  return (
    <div className={cn(
      "relative flex items-center gap-1.5 rounded-full px-2.5 py-1",
      "text-[10px] font-semibold tracking-wide uppercase",
      "border backdrop-blur-sm",
      config.borderColor,
      config.textColor
    )}>
      {/* Animated pulse ring for running state */}
      {isRunning && (
        <>
          <span className="absolute inset-0 rounded-full animate-ping opacity-20 bg-blue-400" />
          <span className="absolute inset-0 rounded-full animate-pulse opacity-10 bg-blue-500" />
        </>
      )}

      {/* Status dot with animation */}
      <span className="relative flex h-1.5 w-1.5">
        {isRunning && (
          <span className={cn(
            "absolute inline-flex h-full w-full rounded-full opacity-75 animate-ping",
            config.pulseColor
          )} />
        )}
        <span className={cn(
          "relative inline-flex rounded-full h-1.5 w-1.5",
          config.pulseColor
        )} />
      </span>

      {t(config.labelKey, config.label)}
    </div>
  );
}

// Chevron icon with rotation animation
function ChevronIcon({ isExpanded }: { isExpanded: boolean }) {
  return (
    <motion.svg
      animate={{ rotate: isExpanded ? 90 : 0 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      className="h-4 w-4 text-muted-foreground/60"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
    </motion.svg>
  );
}

export function ToolCallBlock({ toolCall, result, argsText, argsValid }: ToolCallBlockProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(false);
  const status = toolCall.status || "pending";
  const config = statusConfig[status] || statusConfig.pending;

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
  const resultPreview = typeof parsedResult === "object" && parsedResult !== null
    ? JSON.stringify(parsedResult)
    : (result ? String(result) : "");
  const previewSource = argsPreview || resultPreview || (rawArgs ? t("playground.toolCall.paramsPending", "Parameters pending...") : "");
  const preview = previewSource.length > 120 ? `${previewSource.slice(0, 120)}...` : previewSource;

  const isRunning = status === "running";

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "relative rounded-xl border overflow-hidden",
        "bg-gradient-to-br backdrop-blur-sm",
        "transition-all duration-300",
        config.bgGradient,
        config.borderColor,
        isExpanded ? "shadow-lg" : "shadow-sm hover:shadow-md"
      )}
    >
      {/* Animated scan line for running state */}
      {isRunning && (
        <div className="absolute inset-0 overflow-hidden pointer-events-none">
          <div className="absolute inset-0 bg-gradient-to-b from-blue-400/0 via-blue-400/10 to-blue-400/0 h-8 animate-[scan_2s_ease-in-out_infinite]" />
        </div>
      )}

      {/* Header */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className={cn(
          "flex w-full items-center gap-3 px-4 py-3 text-left",
          "transition-colors duration-200",
          "hover:bg-white/5 dark:hover:bg-white/5"
        )}
        type="button"
      >
        <ChevronIcon isExpanded={isExpanded} />

        {/* Tool info */}
        <div className="flex min-w-0 flex-1 items-center gap-3">
          {/* Tool icon */}
          <div className={cn(
            "flex h-8 w-8 items-center justify-center rounded-lg",
            "transition-all duration-300",
            config.iconBg,
            config.textColor
          )}>
            <ToolIcon status={status} />
          </div>

          {/* Tool name and preview */}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <span className="font-mono text-sm font-semibold text-foreground truncate">
                {toolCall.name || t("playground.toolCall.unknownTool", "Unknown tool")}
              </span>
            </div>

            {!isExpanded && preview && (
              <p className="mt-0.5 text-xs text-muted-foreground/70 truncate font-mono">
                {preview}
              </p>
            )}
          </div>
        </div>

        <StatusBadge status={status} />
      </button>

      {/* Expandable content */}
      <AnimatePresence>
        {isExpanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: "easeOut" }}
            className="overflow-hidden"
          >
            <div className="border-t border-inherit bg-black/5 dark:bg-white/5 min-w-0 overflow-hidden">
              {/* Arguments section */}
              {rawArgs && (
                <div className="border-b border-inherit">
                  <div className="px-4 py-2 min-w-0 overflow-hidden">
                    <div className="flex items-center gap-2 mb-2">
                      <svg className="h-3.5 w-3.5 text-muted-foreground/60 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M17 8l4 4m0 0l-4 4m4-4H3" />
                      </svg>
                      <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/80">
                        {hasValidArgs
                          ? t("playground.toolCall.arguments", "Arguments")
                          : t("playground.toolCall.argumentsPartial", "Arguments (streaming...)")}
                      </span>
                    </div>
                    <div className="relative min-w-0 overflow-hidden">
                      <pre className={cn(
                        "max-h-48 overflow-auto rounded-lg p-3",
                        "bg-black/10 dark:bg-black/30",
                        "text-xs font-mono text-foreground/90",
                        "border border-white/5",
                        "whitespace-pre-wrap break-words"
                      )}>
                        {parsedArgs ? JSON.stringify(parsedArgs, null, 2) : rawArgs}
                      </pre>
                      {!hasValidArgs && (
                        <div className="absolute bottom-2 right-2">
                          <span className="flex h-2 w-2">
                            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75" />
                            <span className="relative inline-flex rounded-full h-2 w-2 bg-blue-500" />
                          </span>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              )}

              {/* Result section */}
              {result && (
                <div className="px-4 py-2 min-w-0 overflow-hidden">
                  <div className="flex items-center gap-2 mb-2">
                    <svg className="h-3.5 w-3.5 text-emerald-500/80 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <span className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground/80">
                      {t("playground.toolCall.result", "Result")}
                    </span>
                  </div>
                  <pre className={cn(
                    "max-h-48 overflow-auto rounded-lg p-3",
                    "bg-emerald-500/5 dark:bg-emerald-500/10",
                    "text-xs font-mono text-foreground/90",
                    "border border-emerald-500/20",
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

      {/* Add scan animation keyframes */}
      <style>{`
        @keyframes scan {
          0%, 100% { transform: translateY(-100%); }
          50% { transform: translateY(400%); }
        }
      `}</style>
    </motion.div>
  );
}
