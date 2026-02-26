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

function isNoiseStepLabel(label: string | undefined): boolean {
  if (!label) return false;
  const normalized = label.trim().toLowerCase();
  if (!normalized) return false;
  return /(正在|执行).{0,24}0\s*个?\s*工具/u.test(normalized) ||
    /(running|executing).{0,24}\b0\s*tools?\b/u.test(normalized);
}

function normalizeStepLabel(label: string | undefined): string | undefined {
  if (!label) return undefined;
  const trimmed = label.trim();
  if (!trimmed || isNoiseStepLabel(trimmed)) return undefined;
  return trimmed;
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

  const visibleCurrentStep = normalizeStepLabel(summary.currentStep);
  const visibleSteps = useMemo(
    () =>
      summary.steps
        .filter((step) => !isNoiseStepLabel(step.title))
        .map((step) => ({
          ...step,
          title: normalizeStepLabel(step.title) ?? step.id,
        })),
    [summary.steps]
  );

  const stepTotal = visibleSteps.length;
  const stepCompleted = visibleSteps.filter((s) => s.status === "completed").length;
  const toolTotal = summary.tools.length;
  const toolRunning = summary.tools.filter((s) => s.status === "running").length;
  const hasContextBudget = Boolean(summary.contextBudget);
  const hasContextCompacted = Boolean(summary.contextCompacted?.compacted);
  const canExpand = stepTotal > 0 || toolTotal > 0 || hasContextBudget || hasContextCompacted;
  const hasError =
    summary.status === "failed" ||
    summary.steps.some((s) => s.status === "failed") ||
    summary.tools.some((s) => s.status === "error");
  const durationMs = summary.totalDurationMs;

  const headerText = useMemo(() => {
    if (hasError) return t("assistant.processSummary.failed", "Execution failed");
    if (summary.status === "succeeded") {
      if (toolTotal === 0 && stepTotal === 0) {
        return t("assistant.processSummary.completedSimple", "Completed");
      }
      return t("assistant.processSummary.completed", "{{tools}} tools · {{steps}} steps", {
        tools: toolTotal,
        steps: stepTotal,
      });
    }
    if (visibleCurrentStep) {
      return t("assistant.processSummary.runningStep", "Running: {{step}}", {
        step: visibleCurrentStep,
      });
    }
    if (toolRunning === 0 && toolTotal === 0) {
      return t("assistant.processSummary.preparing", "Preparing response");
    }
    return t("assistant.processSummary.running", "Running {{tools}} tools", {
      tools: toolRunning || toolTotal,
    });
  }, [hasError, summary.status, visibleCurrentStep, toolTotal, stepTotal, toolRunning, t]);

  const headerIcon = hasError ? (
    <AlertCircle className="h-4 w-4 text-red-500" />
  ) : summary.status === "succeeded" ? (
    <CheckCircle2 className="h-4 w-4 text-emerald-500" />
  ) : (
    <Loader2 className="h-4 w-4 text-[hsl(var(--assistant-accent))] animate-spin" />
  );

  if (summary.status === "succeeded" && !canExpand) {
    return null;
  }

  return (
    <div
      className={cn(
        "w-full rounded-lg",
        hasError
          ? "bg-red-500/5"
          : "bg-transparent"
      )}
    >
      {canExpand ? (
        <button
          type="button"
          onClick={() => setUserExpanded((current) => !(current ?? defaultExpanded))}
          className="w-full py-1.5 pr-1 flex items-center gap-2 text-left"
          aria-expanded={expanded}
        >
          {headerIcon}
          <span className="text-xs sm:text-sm text-[hsl(var(--assistant-text-secondary))] flex-1 truncate">
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
      ) : (
        <div className="w-full py-1.5 pr-1 flex items-center gap-2 text-left">
          {headerIcon}
          <span className="text-xs sm:text-sm text-[hsl(var(--assistant-text-secondary))] flex-1 truncate">
            {headerText}
          </span>
          {durationMs != null && durationMs > 0 && (
            <span className="hidden sm:inline-flex items-center gap-1 text-[11px] text-[hsl(var(--assistant-text-secondary))]">
              <Clock3 className="h-3 w-3" />
              {(durationMs / 1000).toFixed(1)}s
            </span>
          )}
        </div>
      )}

      <AnimatePresence initial={false}>
        {expanded && canExpand && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
            className="overflow-hidden"
          >
            <div className="ml-5 pl-3 pb-2 border-l border-[hsl(var(--assistant-border-soft))]/80 space-y-2">
              {stepTotal > 0 && (
                <div>
                  <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wide text-[hsl(var(--assistant-text-secondary))] mb-1.5">
                    <ListTodo className="h-3 w-3" />
                    {t("assistant.processSummary.steps", "Steps")} ({stepCompleted}/{stepTotal})
                  </div>
                  <div className="space-y-1">
                    {visibleSteps.map((step) => (
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
                <div>
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

              {hasContextBudget && summary.contextBudget && (
                <div className="text-xs text-[hsl(var(--assistant-text-secondary))]">
                  {t("assistant.processSummary.contextBudget", "Context used")}:{" "}
                  {summary.contextBudget.used_tokens ?? "-"}
                  {summary.contextBudget.model_context_window
                    ? ` / ${summary.contextBudget.model_context_window}`
                    : ""}
                  {typeof summary.contextBudget.dropped_history_messages === "number"
                    ? ` · ${t("assistant.processSummary.historyDropped", "dropped")} ${summary.contextBudget.dropped_history_messages}`
                    : ""}
                </div>
              )}

              {hasContextCompacted && (
                <div className="text-xs text-[hsl(var(--assistant-text-secondary))]">
                  {t("assistant.processSummary.contextCompacted", "Context compacted")}
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
