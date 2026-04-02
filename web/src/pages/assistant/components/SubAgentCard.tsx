/**
 * SubAgentCard — Claude Desktop-inspired sub-agent execution display.
 * Collapsible card with tool timeline and result rendering.
 */
import React, { useState } from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { SubAgentState } from "../types";
import { formatDuration } from "@/lib/format";

const TYPE_META: Record<string, { icon: string; label: string; bg: string; border: string; ring: string }> = {
  explore: { icon: "🔍", label: "Explore", bg: "bg-blue-50 dark:bg-blue-950/20", border: "border-blue-200 dark:border-blue-800/40", ring: "border-blue-400" },
  task:    { icon: "⚙️", label: "Task",    bg: "bg-violet-50 dark:bg-violet-950/20", border: "border-violet-200 dark:border-violet-800/40", ring: "border-violet-400" },
  plan:    { icon: "📋", label: "Plan",    bg: "bg-emerald-50 dark:bg-emerald-950/20", border: "border-emerald-200 dark:border-emerald-800/40", ring: "border-emerald-400" },
};

export function SubAgentCard({ subAgent }: { subAgent: SubAgentState }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(true);
  const meta = TYPE_META[subAgent.agentType] || TYPE_META.explore;
  const isRunning = subAgent.status === "running";
  const isDone = subAgent.status === "completed";
  const isFailed = subAgent.status === "failed";

  return (
    <div className={`rounded-xl border ${meta.border} ${meta.bg} overflow-hidden transition-all my-2`}>
      {/* Header */}
      <button
        className="w-full flex items-center gap-2.5 px-3.5 py-2.5 text-left hover:bg-black/[0.02] dark:hover:bg-white/[0.02] transition-colors"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="text-base shrink-0">{meta.icon}</span>
        <span className="text-[13px] font-semibold text-foreground">{meta.label}</span>
        <span className="text-[13px] text-muted-foreground truncate flex-1">{subAgent.description}</span>

        <span className="flex items-center gap-1.5 shrink-0">
          {isRunning && <span className={`w-2 h-2 rounded-full ${meta.ring} border-[1.5px] border-t-transparent animate-spin`} />}
          {isDone && <span className="text-green-500 text-xs">✓</span>}
          {isFailed && <span className="text-red-500 text-xs">✗</span>}
          {subAgent.durationMs != null && (
            <span className="text-[11px] text-muted-foreground tabular-nums">{formatDuration(subAgent.durationMs)}</span>
          )}
          <svg className={`w-3 h-3 text-muted-foreground/50 transition-transform ${expanded ? "rotate-180" : ""}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </span>
      </button>

      {/* Content */}
      {expanded && (
        <div className="px-3.5 pb-3 space-y-2">
          {/* Tool steps */}
          {subAgent.steps.length > 0 && (
            <div className="space-y-px">
              {subAgent.steps.map((step) => (
                <div key={step.callId} className="flex items-center gap-2 py-0.5 text-[12px]">
                  <span className="w-3 text-center shrink-0">
                    {step.status === "running" ? <span className="text-blue-500">→</span>
                      : step.status === "completed" ? <span className="text-green-500">✓</span>
                      : <span className="text-red-500">✗</span>}
                  </span>
                  <code className="text-[11px] text-muted-foreground font-mono">{step.toolName}</code>
                  {step.summary && (
                    <span className="text-muted-foreground/50 truncate flex-1 text-[11px]">{step.summary}</span>
                  )}
                  {step.durationMs != null && (
                    <span className="text-muted-foreground/40 text-[10px] tabular-nums shrink-0">{formatDuration(step.durationMs)}</span>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Streaming text */}
          {subAgent.streamingText && isRunning && (
            <div className="p-2 rounded-lg bg-black/[0.03] dark:bg-white/[0.03] text-[12px] font-mono max-h-[120px] overflow-y-auto text-muted-foreground/70 leading-relaxed">
              {subAgent.streamingText.slice(-400)}
            </div>
          )}

          {/* Result */}
          {subAgent.resultSummary && isDone && (
            <div className="p-2.5 rounded-lg bg-white/80 dark:bg-slate-800/50 border border-slate-200/50 dark:border-slate-700/30 text-[13px] leading-relaxed prose prose-sm dark:prose-invert max-w-none max-h-[250px] overflow-y-auto">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {subAgent.resultSummary.length > 600 ? subAgent.resultSummary.slice(0, 600) + "\n\n..." : subAgent.resultSummary}
              </ReactMarkdown>
            </div>
          )}

          {/* Error */}
          {subAgent.error && isFailed && (
            <div className="p-2 rounded-lg bg-red-50 dark:bg-red-950/20 text-[12px] text-red-600 dark:text-red-400">
              {subAgent.error}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
