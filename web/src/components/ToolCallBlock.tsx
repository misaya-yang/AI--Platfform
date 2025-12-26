import { useState } from "react";
import { cn } from "@/lib/utils";
import type { ToolCall } from "@/types/gateway";

interface ToolCallBlockProps {
  toolCall: ToolCall;
  result?: string;
  argsText?: string;
  argsValid?: boolean;
}

const statusLabels: Record<string, string> = {
  pending: "Pending",
  running: "Running",
  completed: "Done",
  error: "Error",
};

const statusColors: Record<string, string> = {
  pending: "bg-amber-500/10 text-amber-600 border-amber-500/20",
  running: "bg-blue-500/10 text-blue-600 border-blue-500/20",
  completed: "bg-emerald-500/10 text-emerald-600 border-emerald-500/20",
  error: "bg-red-500/10 text-red-600 border-red-500/20",
};

const statusIcons: Record<string, JSX.Element> = {
  pending: (
    <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
    </svg>
  ),
  running: (
    <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  ),
  completed: (
    <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
    </svg>
  ),
  error: (
    <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
    </svg>
  ),
};

export function ToolCallBlock({ toolCall, result, argsText, argsValid }: ToolCallBlockProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  const rawArgs = argsText ?? toolCall.arguments ?? "";
  const hasValidArgs = argsValid ?? (rawArgs ? (() => {
    try {
      JSON.parse(rawArgs);
      return true;
    } catch {
      return false;
    }
  })() : false);
  const argsValue = hasValidArgs ? rawArgs : "";

  let parsedArgs: Record<string, unknown> | null = null;
  if (argsValue) {
    try {
      parsedArgs = JSON.parse(argsValue);
    } catch {
      parsedArgs = null;
    }
  }

  let parsedResult: unknown = result;
  try {
    if (result) {
      parsedResult = JSON.parse(result);
    }
  } catch {
    // Keep raw string if parsing fails.
  }

  const argsPreview = parsedArgs ? JSON.stringify(parsedArgs) : (argsValue || rawArgs);
  const resultPreview = typeof parsedResult === "object" && parsedResult !== null
    ? JSON.stringify(parsedResult)
    : (result ? String(result) : "");
  const previewSource = argsPreview || resultPreview || (rawArgs ? "Parameters pending..." : "");
  const preview = previewSource.length > 160 ? `${previewSource.slice(0, 160)}...` : previewSource;

  const statusClass = statusColors[toolCall.status] || "bg-muted text-muted-foreground border-border/50";
  const statusLabel = statusLabels[toolCall.status] || toolCall.status;
  const statusIcon = statusIcons[toolCall.status];

  return (
    <div className="rounded-lg border border-border/50 bg-background/60 shadow-sm transition-shadow hover:shadow-md">
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="flex w-full items-start gap-3 px-3 py-2 text-left"
        type="button"
      >
        <svg
          className={cn(
            "mt-1 h-4 w-4 text-muted-foreground transition-transform",
            isExpanded && "rotate-90"
          )}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>

        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div className="flex items-center gap-2">
            <div className="flex h-6 w-6 items-center justify-center rounded bg-violet-500/10 text-violet-600">
              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
                />
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
              </svg>
            </div>
            <span className="min-w-0 truncate font-mono text-xs font-semibold">
              {toolCall.name || "Unknown tool"}
            </span>
          </div>

          {!isExpanded && preview && (
            <div className="truncate text-[11px] text-muted-foreground">
              {preview}
            </div>
          )}
        </div>

        <span
          className={cn(
            "mt-1 flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium",
            statusClass
          )}
        >
          {statusIcon}
          {statusLabel}
        </span>
      </button>

      {isExpanded && (
        <div className="border-t border-border/50 bg-muted/20">
          {rawArgs && (
            <div className="border-b border-border/40 px-3 py-2">
              <div className="mb-1 flex items-center gap-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16l-4-4m0 0l4-4m-4 4h18" />
                </svg>
                {hasValidArgs ? "Arguments" : "Arguments (partial)"}
              </div>
              <pre className="max-h-40 overflow-x-auto overflow-y-auto rounded bg-muted/50 p-2 text-xs font-mono">
                {parsedArgs ? JSON.stringify(parsedArgs, null, 2) : rawArgs}
              </pre>
              {!hasValidArgs && (
                <div className="mt-1 text-[10px] text-muted-foreground">
                  Arguments are still streaming.
                </div>
              )}
            </div>
          )}

          {result && (
            <div className="px-3 py-2">
              <div className="mb-1 flex items-center gap-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 8l4 4m0 0l-4 4m4-4H3" />
                </svg>
                Result
              </div>
              <pre className="max-h-40 overflow-x-auto overflow-y-auto rounded bg-muted/50 p-2 text-xs font-mono">
                {typeof parsedResult === "object" ? JSON.stringify(parsedResult, null, 2) : String(result)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
