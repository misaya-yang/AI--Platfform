import { motion } from "framer-motion";
import { cn } from "@/lib/utils";
import { StreamOutput } from "@/components/StreamOutput";
import { ToolCallBlock } from "@/components/ToolCallBlock";
import type { ToolCall } from "@/types/gateway";
import { Bot, User } from "lucide-react";

export interface ToolCallWithResult {
  toolCall: ToolCall;
  result?: string;
}

export type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  toolCalls?: ToolCallWithResult[];
  isThinking?: boolean; // 显示"AI思考中"状态
  isStreaming?: boolean;
  // 统计信息
  stats?: {
    durationMs?: number;     // 响应耗时（毫秒）
    inputTokens?: number;    // 输入 tokens
    outputTokens?: number;   // 输出 tokens
    totalTokens?: number;    // 总 tokens
    firstTokenMs?: number;   // 首 token 延迟
  };
};

export function ChatWindow({ messages }: { messages: ChatMessage[] }) {
  return (
    <div className="mx-auto w-full max-w-4xl space-y-6 px-4 py-8">
      {messages.map((m, i) => {
        const isUser = m.role === "user";
        const hasToolCalls = !isUser && m.toolCalls && m.toolCalls.length > 0;
        return (
          <motion.div
            key={i}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, ease: "easeOut" }}
            className={cn("flex w-full gap-4", isUser ? "flex-row-reverse" : "flex-row")}
          >
            {/* Avatar */}
            <div className={cn(
              "flex h-8 w-8 shrink-0 items-center justify-center rounded-full border shadow-sm",
              isUser ? "bg-primary text-primary-foreground" : "bg-white dark:bg-zinc-800"
            )}>
              {isUser ? <User className="h-5 w-5" /> : <Bot className="h-5 w-5 text-indigo-500" />}
            </div>

            {/* Content Bubble */}
            <div className={cn(
              "flex max-w-[85%] flex-col gap-2",
              isUser ? "items-end" : "items-start"
            )}>
              {/* Name Label */}
              <span className="text-xs text-muted-foreground ml-1">
                {isUser ? "You" : "AI Assistant"}
              </span>

              <div
                className={cn(
                  "relative rounded-2xl px-5 py-3.5 text-sm shadow-sm",
                  isUser
                    ? "bg-primary text-primary-foreground rounded-tr-sm"
                    : "bg-white dark:bg-zinc-900 border border-border/50 rounded-tl-sm"
                )}
              >
                {!isUser && hasToolCalls && (
                  <div className="mb-3 rounded-xl border border-border/50 bg-muted/40 p-2">
                    <div className="mb-2 flex items-center gap-2 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
                      <span className="h-1.5 w-1.5 rounded-full bg-indigo-400" />
                      Tool Calls
                    </div>
                    <div className="space-y-2">
                      {m.toolCalls?.map((tc, idx) => (
                        <ToolCallBlock
                          key={tc.toolCall.tool_call_id || idx}
                          toolCall={tc.toolCall}
                          result={tc.result}
                        />
                      ))}
                    </div>
                  </div>
                )}
                {m.content ? (
                  <div className={cn("leading-relaxed", isUser ? "text-white" : "text-foreground")}>
                    {isUser ? <div className="whitespace-pre-wrap">{m.content}</div> : (
                      <StreamOutput text={m.content} isStreaming={!!m.isStreaming} enableTypingEffect={false} />
                    )}
                  </div>
                ) : null}

                {/* AI Thinking / Loading Indicator */}
                {!m.content && !hasToolCalls && !isUser && (
                  <div className="flex items-center gap-2 h-6">
                    {m.isThinking ? (
                      <>
                        <span className="text-sm text-muted-foreground">Thinking...</span>
                        <div className="flex items-center gap-1">
                          <div className="h-1.5 w-1.5 animate-bounce rounded-full bg-indigo-500 [animation-delay:-0.3s]" />
                          <div className="h-1.5 w-1.5 animate-bounce rounded-full bg-indigo-500 [animation-delay:-0.15s]" />
                          <div className="h-1.5 w-1.5 animate-bounce rounded-full bg-indigo-500" />
                        </div>
                      </>
                    ) : (
                      <div className="flex items-center gap-1.5">
                        <div className="h-1.5 w-1.5 animate-bounce rounded-full bg-indigo-500 [animation-delay:-0.3s]" />
                        <div className="h-1.5 w-1.5 animate-bounce rounded-full bg-indigo-500 [animation-delay:-0.15s]" />
                        <div className="h-1.5 w-1.5 animate-bounce rounded-full bg-indigo-500" />
                      </div>
                    )}
                  </div>
                )}
              </div>

              {/* Stats (仅助手消息显示) */}
              {!isUser && m.stats && !m.isThinking && m.content && (
                <div className="flex items-center gap-3 text-[10px] text-muted-foreground/60 mt-1 ml-1">
                  {m.stats.durationMs != null && (
                    <span className="flex items-center gap-1">
                      <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <circle cx="12" cy="12" r="10"/>
                        <polyline points="12 6 12 12 16 14"/>
                      </svg>
                      {(m.stats.durationMs / 1000).toFixed(2)}s
                    </span>
                  )}
                  {m.stats.firstTokenMs != null && (
                    <span>TTFT: {m.stats.firstTokenMs}ms</span>
                  )}
                  {m.stats.totalTokens != null && (
                    <span className="flex items-center gap-1">
                      <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
                      </svg>
                      {m.stats.totalTokens} tokens
                      {m.stats.inputTokens != null && m.stats.outputTokens != null && (
                        <span className="text-muted-foreground/40">
                          ({m.stats.inputTokens} in / {m.stats.outputTokens} out)
                        </span>
                      )}
                    </span>
                  )}
                </div>
              )}
            </div>
          </motion.div>
        );
      })}
    </div>
  );
}
