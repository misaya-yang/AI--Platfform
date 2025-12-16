import { useState } from "react";
import { cn } from "@/lib/utils";
import type { ToolCall } from "@/types/gateway";

interface ToolCallBlockProps {
  toolCall: ToolCall;
  result?: string;
}

const statusLabels: Record<string, string> = {
  pending: "等待中",
  running: "执行中",
  completed: "已完成",
  error: "错误",
};

export function ToolCallBlock({ toolCall, result }: ToolCallBlockProps) {
  const [isExpanded, setIsExpanded] = useState(false);

  // 解析参数
  let parsedArgs: Record<string, unknown> | null = null;
  try {
    if (toolCall.arguments) {
      parsedArgs = JSON.parse(toolCall.arguments);
    }
  } catch {
    // 保持原始字符串
  }

  // 解析结果
  let parsedResult: unknown = result;
  try {
    if (result) {
      parsedResult = JSON.parse(result);
    }
  } catch {
    // 保持原始字符串
  }

  const statusColors = {
    pending: "bg-amber-500/10 text-amber-600 border-amber-500/20",
    running: "bg-blue-500/10 text-blue-600 border-blue-500/20",
    completed: "bg-emerald-500/10 text-emerald-600 border-emerald-500/20",
    error: "bg-red-500/10 text-red-600 border-red-500/20",
  };

  const statusIcons = {
    pending: (
      <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
      </svg>
    ),
    running: (
      <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
      </svg>
    ),
    completed: (
      <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
      </svg>
    ),
    error: (
      <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
      </svg>
    ),
  };

  return (
    <div className="my-2 rounded-xl border border-border/40 bg-white/50 dark:bg-black/30 backdrop-blur-sm overflow-hidden shadow-sm transition-all hover:shadow-md">
      {/* 头部 */}
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center gap-2 px-3 py-2 hover:bg-muted/50 transition-colors text-left"
      >
        {/* 展开/折叠图标 */}
        <svg
          className={cn(
            "w-4 h-4 text-muted-foreground transition-transform",
            isExpanded && "rotate-90"
          )}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>

        {/* 工具图标 */}
        <div className="flex items-center justify-center w-6 h-6 rounded bg-violet-500/10 text-violet-600">
          <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
            />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
        </div>

        {/* 工具名称 */}
        <span className="font-mono text-sm font-medium flex-1 truncate">
          {toolCall.name || "未知工具"}
        </span>

        {/* 状态标签 */}
        <span
          className={cn(
            "flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border",
            statusColors[toolCall.status]
          )}
        >
          {statusIcons[toolCall.status]}
          {statusLabels[toolCall.status] || toolCall.status}
        </span>
      </button>

      {/* 展开内容 */}
      {isExpanded && (
        <div className="border-t bg-background/50">
          {/* 参数 */}
          {toolCall.arguments && (
            <div className="px-3 py-2 border-b">
              <div className="text-xs font-medium text-muted-foreground mb-1 flex items-center gap-1">
                <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 16l-4-4m0 0l4-4m-4 4h18" />
                </svg>
                输入参数
              </div>
              <pre className="text-xs font-mono bg-muted/50 rounded p-2 overflow-x-auto max-h-40 overflow-y-auto">
                {parsedArgs
                  ? JSON.stringify(parsedArgs, null, 2)
                  : toolCall.arguments}
              </pre>
            </div>
          )}

          {/* 结果 */}
          {result && (
            <div className="px-3 py-2">
              <div className="text-xs font-medium text-muted-foreground mb-1 flex items-center gap-1">
                <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 8l4 4m0 0l-4 4m4-4H3" />
                </svg>
                执行结果
              </div>
              <pre className="text-xs font-mono bg-muted/50 rounded p-2 overflow-x-auto max-h-40 overflow-y-auto">
                {typeof parsedResult === "object"
                  ? JSON.stringify(parsedResult, null, 2)
                  : String(result)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
