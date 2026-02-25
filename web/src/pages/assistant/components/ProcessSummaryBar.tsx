import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import {
  ChevronDown,
  CheckCircle2,
  Loader2,
  AlertCircle,
  Wrench,
  Clock3,
  ListTodo,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { ProcessSummaryState, ToolTimelineItem } from "../types";

interface ProcessSummaryBarProps {
  summary: ProcessSummaryState;
}

function renderToolSummary(tool: ToolTimelineItem): string {
  if (tool.error) return tool.error;
  if (tool.summary) return tool.summary;
  if (tool.queueState) return `queue: ${tool.queueState}`;
  return "";
}

export function ProcessSummaryBar({ summary }: ProcessSummaryBarProps) {
  const { t } = useTranslation();
  const [userExpanded, setUserExpanded] = useState<boolean | null>(null);
  const defaultExpanded = summary.isErrorExpanded === true || !summary.collapsed;
  const expanded = summary.isErrorExpanded === true ? true : (userExpanded ?? defaultExpanded);

  const stepTotal = summary.steps.length;
  const stepCompleted = summary.steps.filter((s) => s.status === "completed").length;
  const toolTotal = summary.tools.length;
  const toolRunning = summary.tools.filter((s) => s.status === "running").length;
  const hasError =
    summary.status === "failed" ||
    summary.steps.some((s) => s.status === "failed") ||
    summary.tools.some((s) => s.status === "error");
  const durationMs = summary.totalDurationMs;

  const headerText = useMemo(() => {
    if (hasError) return t("assistant.processSummary.failed", "Execution failed");
    if (summary.status === "succeeded") {
      return t("assistant.processSummary.completed", "{{tools}} tools · {{steps}} steps", {
        tools: toolTotal,
        steps: stepTotal,
      });
    }
    if (summary.currentStep) {
      return t("assistant.processSummary.runningStep", "Running: {{step}}", {
        step: summary.currentStep,
      });
    }
    return t("assistant.processSummary.running", "Running {{tools}} tools", {
      tools: toolRunning || toolTotal,
    });
  }, [hasError, summary.status, summary.currentStep, toolTotal, stepTotal, toolRunning, t]);

  const headerIcon = hasError ? (
    <AlertCircle className="h-4 w-4 text-red-500" />
  ) : summary.status === "succeeded" ? (
    <CheckCircle2 className="h-4 w-4 text-emerald-500" />
  ) : (
    <Loader2 className="h-4 w-4 text-blue-500 animate-spin" />
  );

  return (
    <div className="rounded-xl border border-[hsl(var(--assistant-border-soft))] bg-[hsl(var(--assistant-chip-bg))]/60">
      <button
        type="button"
        onClick={() => setUserExpanded((current) => !(current ?? defaultExpanded))}
        className="w-full px-3 py-2.5 flex items-center gap-2 text-left"
        aria-expanded={expanded}
      >
        {headerIcon}
        <span className="text-xs sm:text-sm font-medium text-[hsl(var(--assistant-text-primary))] flex-1 truncate">
          {headerText}
        </span>
        {durationMs != null && durationMs > 0 && (
          <span className="hidden sm:inline-flex items-center gap-1 text-[11px] text-[hsl(var(--assistant-text-secondary))]">
            <Clock3 className="h-3 w-3" />
            {(durationMs / 1000).toFixed(1)}s
          </span>
        )}
        <motion.span animate={{ rotate: expanded ? 180 : 0 }} transition={{ duration: 0.15 }}>
          <ChevronDown className="h-4 w-4 text-[hsl(var(--assistant-text-secondary))]" />
        </motion.span>
      </button>

      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
            className="overflow-hidden"
          >
            <div className="px-3 pb-3 space-y-2">
              {stepTotal > 0 && (
                <div className="rounded-lg border border-[hsl(var(--assistant-border-soft))] bg-background/60 p-2">
                  <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-[hsl(var(--assistant-text-secondary))] mb-1.5">
                    <ListTodo className="h-3 w-3" />
                    {t("assistant.processSummary.steps", "Steps")} ({stepCompleted}/{stepTotal})
                  </div>
                  <div className="space-y-1">
                    {summary.steps.map((step) => (
                      <div key={step.id} className="flex items-center gap-2 text-xs">
                        <span
                          className={cn(
                            "h-1.5 w-1.5 rounded-full",
                            step.status === "completed" && "bg-emerald-500",
                            step.status === "running" && "bg-blue-500",
                            step.status === "failed" && "bg-red-500",
                            step.status === "pending" && "bg-slate-400"
                          )}
                        />
                        <span className="text-[hsl(var(--assistant-text-primary))] truncate">{step.title}</span>
                        {step.durationMs != null && (
                          <span className="ml-auto text-[11px] text-[hsl(var(--assistant-text-secondary))]">
                            {(step.durationMs / 1000).toFixed(1)}s
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {toolTotal > 0 && (
                <div className="rounded-lg border border-[hsl(var(--assistant-border-soft))] bg-background/60 p-2">
                  <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-[hsl(var(--assistant-text-secondary))] mb-1.5">
                    <Wrench className="h-3 w-3" />
                    {t("assistant.processSummary.tools", "Tools")} ({toolTotal})
                  </div>
                  <div className="space-y-1.5">
                    {summary.tools.map((tool) => {
                      const toolSummary = renderToolSummary(tool);
                      return (
                        <div key={tool.id} className="text-xs">
                          <div className="flex items-center gap-2">
                            <span
                              className={cn(
                                "h-1.5 w-1.5 rounded-full",
                                tool.status === "completed" && "bg-emerald-500",
                                tool.status === "running" && "bg-blue-500",
                                tool.status === "error" && "bg-red-500",
                                tool.status === "approval_required" && "bg-amber-500",
                                tool.status === "pending" && "bg-slate-400"
                              )}
                            />
                            <span className="font-mono text-[hsl(var(--assistant-text-primary))] truncate">
                              {tool.name}
                            </span>
                            {tool.durationMs != null && (
                              <span className="ml-auto text-[11px] text-[hsl(var(--assistant-text-secondary))]">
                                {(tool.durationMs / 1000).toFixed(1)}s
                              </span>
                            )}
                          </div>
                          {toolSummary && (
                            <div className="ml-3.5 mt-0.5 text-[11px] text-[hsl(var(--assistant-text-secondary))] truncate">
                              {toolSummary}
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
