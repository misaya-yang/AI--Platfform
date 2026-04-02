/**
 * SubAgentCard — displays a sub-agent's execution progress and results.
 * ADR-003: Collapsible card with tool call timeline and streaming output.
 */
import React, { useState } from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { SubAgentState } from "../types";

const AGENT_TYPE_CONFIG: Record<string, { icon: string; label: string; color: string }> = {
  explore: { icon: "🔍", label: "Explore", color: "border-blue-300 bg-blue-50/50 dark:bg-blue-950/30" },
  task:    { icon: "⚙️", label: "Task",    color: "border-purple-300 bg-purple-50/50 dark:bg-purple-950/30" },
  plan:    { icon: "📋", label: "Plan",    color: "border-green-300 bg-green-50/50 dark:bg-green-950/30" },
};

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function SubAgentCard({ subAgent }: { subAgent: SubAgentState }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(subAgent.status === "running");
  const config = AGENT_TYPE_CONFIG[subAgent.agentType] || AGENT_TYPE_CONFIG.explore;

  const statusIcon = subAgent.status === "running" ? "⏳" : subAgent.status === "completed" ? "✅" : "❌";

  return (
    <div className={`border rounded-xl p-3 my-2 transition-all ${config.color} dark:border-slate-600`}>
      {/* Header */}
      <div className="flex items-center justify-between cursor-pointer select-none" onClick={() => setExpanded(!expanded)}>
        <div className="flex items-center gap-2">
          <span className="text-base">{config.icon}</span>
          <span className="font-medium text-sm">{config.label}</span>
          <span className="text-sm text-muted-foreground">{subAgent.description}</span>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {subAgent.status === "running" && (
            <div className="w-3 h-3 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
          )}
          <span>{statusIcon}</span>
          {subAgent.durationMs != null && <span>{formatDuration(subAgent.durationMs)}</span>}
          <span className="text-[10px]">{expanded ? "▲" : "▼"}</span>
        </div>
      </div>

      {/* Expanded Content */}
      {expanded && (
        <div className="mt-2 ml-6 space-y-1.5">
          {/* Tool call steps */}
          {subAgent.steps.length > 0 && (
            <div className="space-y-0.5">
              {subAgent.steps.map((step) => (
                <div key={step.callId} className="flex items-center gap-2 text-xs">
                  <span className="w-3 text-center">
                    {step.status === "running" ? "→" : step.status === "completed" ? "✓" : "✗"}
                  </span>
                  <span className="font-mono text-muted-foreground">{step.toolName}</span>
                  {step.summary && (
                    <span className="text-muted-foreground/60 truncate max-w-[250px]">{step.summary}</span>
                  )}
                  {step.durationMs != null && (
                    <span className="text-muted-foreground/50 shrink-0">{formatDuration(step.durationMs)}</span>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* Streaming text output */}
          {subAgent.streamingText && subAgent.status === "running" && (
            <div className="p-2 rounded-lg bg-black/5 dark:bg-white/5 text-xs font-mono max-h-[150px] overflow-y-auto text-muted-foreground">
              {subAgent.streamingText.slice(-400)}
            </div>
          )}

          {/* Result summary — render as Markdown */}
          {subAgent.resultSummary && subAgent.status === "completed" && (
            <div className="p-2 rounded-lg bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 text-sm text-foreground leading-relaxed prose prose-sm dark:prose-invert max-w-none max-h-[300px] overflow-y-auto">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {subAgent.resultSummary.slice(0, 500)}
              </ReactMarkdown>
            </div>
          )}

          {/* Error */}
          {subAgent.error && (
            <div className="p-2 rounded-lg bg-red-50 dark:bg-red-950/30 text-sm text-red-700 dark:text-red-400">
              {subAgent.error}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
