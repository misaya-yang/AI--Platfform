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
};

export function ChatWindow({ messages }: { messages: ChatMessage[] }) {
  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 px-4 py-8">
      {messages.map((m, i) => {
        const isUser = m.role === "user";
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
                {m.content ? (
                  <div className={cn("leading-relaxed", isUser ? "text-white" : "text-foreground")}>
                    {isUser ? <div className="whitespace-pre-wrap">{m.content}</div> : <StreamOutput text={m.content} />}
                  </div>
                ) : null}

                {/* Loading Indicator for empty AI messages */}
                {!m.content && (!m.toolCalls || m.toolCalls.length === 0) && !isUser && (
                  <div className="flex items-center gap-1.5 h-6">
                    <div className="h-1.5 w-1.5 animate-bounce rounded-full bg-indigo-500 [animation-delay:-0.3s]" />
                    <div className="h-1.5 w-1.5 animate-bounce rounded-full bg-indigo-500 [animation-delay:-0.15s]" />
                    <div className="h-1.5 w-1.5 animate-bounce rounded-full bg-indigo-500" />
                  </div>
                )}
              </div>

              {/* Tool Calls Area */}
              {m.toolCalls && m.toolCalls.length > 0 && (
                <div className="w-full space-y-2 mt-1">
                  {m.toolCalls.map((tc, idx) => (
                    <ToolCallBlock
                      key={tc.toolCall.tool_call_id || idx}
                      toolCall={tc.toolCall}
                      result={tc.result}
                    />
                  ))}
                </div>
              )}
            </div>
          </motion.div>
        );
      })}
    </div>
  );
}
