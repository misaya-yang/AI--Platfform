/**
 * Chat Message Component
 *
 * Renders a single message in the chat, with support for:
 * - User/assistant avatars
 * - Streaming content
 * - File attachments
 * - KB context display
 * - Web search results
 * - Usage statistics
 * - Search status display (Phase 1)
 */

import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import { Bot, User, Clock, MessageSquare, FileText, Image as ImageIcon, Database, Globe, Loader2, CheckCircle2, Sparkles, Zap } from "lucide-react";
import { cn } from "@/lib/utils";
import { StreamOutput } from "@/components/StreamOutput";
import { WebSearchDisplay } from "./WebSearchDisplay";
import { ContextDisplay } from "./ContextDisplay";
import { CitationDisplay } from "./CitationDisplay";
import type { ChatMessage as ChatMessageType, SearchStatusItem } from "../types";

interface ChatMessageProps {
  message: ChatMessageType;
}

/** Search status display showing KB/web search progress */
function SearchStatusDisplay({ searchStatus }: { searchStatus: SearchStatusItem[] }) {
  const { t } = useTranslation();

  if (!searchStatus || searchStatus.length === 0) return null;

  return (
    <div className="mb-3 space-y-2">
      <AnimatePresence mode="popLayout">
        {searchStatus.map((item, index) => {
          const isKB = item.type === "kb";
          const isWeb = item.type === "web";
          const isFiles = item.type === "files";
          const isSearching = item.state === "searching";
          const isCompleted = item.state === "completed";

          return (
            <motion.div
              key={`${item.type}-${index}`}
              initial={{ opacity: 0, y: -8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              className={cn(
                "flex items-center gap-2 px-3 py-2 rounded-xl text-xs",
                isSearching
                  ? "bg-slate-100/80 dark:bg-slate-700/50"
                  : isCompleted
                    ? "bg-emerald-50/80 dark:bg-emerald-900/20"
                    : "bg-red-50/80 dark:bg-red-900/20"
              )}
            >
              {/* Icon */}
              <div className="flex-shrink-0">
                {isSearching ? (
                  <Loader2 className={cn(
                    "h-4 w-4 animate-spin",
                    isKB ? "text-emerald-500" : isFiles ? "text-violet-500" : "text-blue-500"
                  )} />
                ) : isCompleted ? (
                  <CheckCircle2 className={cn(
                    "h-4 w-4",
                    isKB ? "text-emerald-500" : isFiles ? "text-violet-500" : "text-blue-500"
                  )} />
                ) : (
                  isKB ? (
                    <Database className="h-4 w-4 text-red-500" />
                  ) : isFiles ? (
                    <FileText className="h-4 w-4 text-red-500" />
                  ) : (
                    <Globe className="h-4 w-4 text-red-500" />
                  )
                )}
              </div>

              {/* Text */}
              <div className="flex-1 min-w-0">
                <span className={cn(
                  "font-medium",
                  isSearching
                    ? "text-slate-700 dark:text-slate-300"
                    : isCompleted
                      ? "text-emerald-700 dark:text-emerald-300"
                      : "text-red-700 dark:text-red-300"
                )}>
                  {isSearching ? (
                    isKB
                      ? t("assistant.searchingKB", "Searching knowledge base...")
                      : isFiles
                        ? t("assistant.processingFiles", "Analyzing uploaded files...")
                        : t("assistant.searchingWeb", "Searching the web...")
                  ) : isCompleted ? (
                    isKB
                      ? t("assistant.kbResultsFound", "Found {{count}} sources", { count: item.resultCount || 0 })
                      : isFiles
                        ? t("assistant.filesProcessed", "Analyzed {{count}} files", { count: item.resultCount || 0 })
                        : t("assistant.webResultsFound", "Found {{count}} results", { count: item.resultCount || 0 })
                  ) : (
                    item.error || t("assistant.searchError", "Search failed")
                  )}
                </span>

                {/* Query preview */}
                {isSearching && item.query && (
                  <span className="ml-1.5 text-slate-500 dark:text-slate-400 truncate">
                    {t("assistant.searchingFor", 'for "{{query}}"', {
                      query: item.query.length > 30 ? `${item.query.slice(0, 30)}...` : item.query
                    })}
                  </span>
                )}

                {/* Dataset info */}
                {isKB && item.datasets && item.datasets.length > 0 && (
                  <span className="ml-1.5 text-slate-400 dark:text-slate-500">
                    ({item.datasets.length === 1
                      ? item.datasets[0]
                      : t("assistant.datasetsCount", "{{count}} datasets", { count: item.datasets.length })
                    })
                  </span>
                )}

                {/* Duration */}
                {!isSearching && item.durationMs !== undefined && (
                  <span className="ml-1.5 text-slate-400 dark:text-slate-500">
                    ({(item.durationMs / 1000).toFixed(2)}s)
                  </span>
                )}
              </div>
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}

/** Stats badge showing token usage and timing */
function StatsBadge({ message }: { message: ChatMessageType }) {
  const { t } = useTranslation();
  if (!message.usage && !message.durationMs) return null;

  const totalTokens =
    (message.usage?.input_tokens || 0) + (message.usage?.output_tokens || 0);

  return (
    <motion.div
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.2 }}
      className="flex flex-wrap items-center gap-2 text-[10px] mt-3"
    >
      {message.durationMs != null && (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-slate-100 dark:bg-slate-800/60 text-slate-600 dark:text-slate-400">
          <Clock className="h-3 w-3" />
          {(message.durationMs / 1000).toFixed(2)}s
        </span>
      )}
      {totalTokens > 0 && (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-slate-100 dark:bg-slate-800/60 text-slate-600 dark:text-slate-400">
          <MessageSquare className="h-3 w-3" />
          {totalTokens} {t("assistant.tokens", "tokens")}
          {message.usage?.input_tokens != null &&
            message.usage?.output_tokens != null && (
              <span className="text-slate-400 dark:text-slate-500 ml-1">
                ({message.usage.input_tokens}↑ / {message.usage.output_tokens}↓)
              </span>
            )}
        </span>
      )}
      {/* Cache metrics */}
      {message.cacheMetrics && message.cacheMetrics.cache_hit_rate > 0 && (
        <span className="flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-100 dark:bg-emerald-900/40 text-emerald-600 dark:text-emerald-400">
          <Zap className="h-3 w-3" />
          {(message.cacheMetrics.cache_hit_rate * 100).toFixed(0)}% cached
          {message.cacheMetrics.estimated_savings_usd > 0.0001 && (
            <span className="text-emerald-500 ml-1">
              (-${message.cacheMetrics.estimated_savings_usd.toFixed(4)})
            </span>
          )}
        </span>
      )}
    </motion.div>
  );
}

/** Attachments display in message */
function AttachmentsDisplay({
  attachments,
}: {
  attachments: ChatMessageType["attachments"];
}) {
  if (!attachments || attachments.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-2 mb-3">
      {attachments.map((att, idx) => (
        <div
          key={idx}
          className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-white/20 backdrop-blur-sm text-xs text-white/90 border border-white/20"
        >
          {att.type === "image" ? (
            <ImageIcon className="h-3.5 w-3.5" />
          ) : (
            <FileText className="h-3.5 w-3.5" />
          )}
          <span className="truncate max-w-[100px]">
            {att.filename || "Attachment"}
          </span>
        </div>
      ))}
    </div>
  );
}

/** GPT-style image generation placeholder */
function ImageGeneratingPlaceholder({ prompt }: { prompt?: string }) {
  const { t } = useTranslation();

  return (
    <div className="space-y-3">
      {/* Status header */}
      <div className="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
        <motion.div
          animate={{ rotate: 360 }}
          transition={{ duration: 2, repeat: Infinity, ease: "linear" }}
        >
          <Sparkles className="h-4 w-4 text-pink-500" />
        </motion.div>
        <span className="font-medium">
          {t("assistant.creatingImage", "正在创建图片")}
        </span>
      </div>

      {/* Image placeholder box - GPT style */}
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        className="relative w-[280px] h-[280px] rounded-2xl overflow-hidden bg-gradient-to-br from-slate-100 to-slate-200 dark:from-slate-700 dark:to-slate-800 border border-slate-200 dark:border-slate-600"
      >
        {/* Shimmer effect */}
        <motion.div
          className="absolute inset-0 bg-gradient-to-r from-transparent via-white/30 to-transparent dark:via-white/10"
          animate={{
            x: ["-100%", "100%"],
          }}
          transition={{
            duration: 1.5,
            repeat: Infinity,
            ease: "linear",
          }}
        />

        {/* Center icon */}
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <motion.div
            animate={{
              scale: [1, 1.1, 1],
              opacity: [0.5, 0.8, 0.5],
            }}
            transition={{
              duration: 2,
              repeat: Infinity,
              ease: "easeInOut",
            }}
            className="p-4 rounded-full bg-slate-200/80 dark:bg-slate-600/50"
          >
            <ImageIcon className="h-10 w-10 text-slate-400 dark:text-slate-500" />
          </motion.div>

          {/* Progress dots */}
          <div className="flex gap-1.5 mt-4">
            <motion.div
              className="w-2 h-2 rounded-full bg-pink-400"
              animate={{ scale: [1, 1.3, 1] }}
              transition={{ duration: 0.8, repeat: Infinity, delay: 0 }}
            />
            <motion.div
              className="w-2 h-2 rounded-full bg-pink-400"
              animate={{ scale: [1, 1.3, 1] }}
              transition={{ duration: 0.8, repeat: Infinity, delay: 0.2 }}
            />
            <motion.div
              className="w-2 h-2 rounded-full bg-pink-400"
              animate={{ scale: [1, 1.3, 1] }}
              transition={{ duration: 0.8, repeat: Infinity, delay: 0.4 }}
            />
          </div>
        </div>
      </motion.div>

      {/* Prompt preview */}
      {prompt && (
        <p className="text-xs text-slate-400 dark:text-slate-500 italic truncate max-w-[280px]">
          "{prompt.length > 50 ? prompt.slice(0, 50) + "..." : prompt}"
        </p>
      )}
    </div>
  );
}

export function ChatMessage({ message }: ChatMessageProps) {
  const { t } = useTranslation();
  const isUser = message.role === "user";

  return (
    <motion.div
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: "easeOut" }}
      className={cn(
        "flex w-full gap-4",
        isUser ? "flex-row-reverse" : "flex-row"
      )}
    >
      {/* Avatar */}
      <motion.div
        initial={{ scale: 0.8 }}
        animate={{ scale: 1 }}
        transition={{ delay: 0.1, type: "spring", stiffness: 200 }}
        className={cn(
          "flex h-10 w-10 shrink-0 items-center justify-center rounded-2xl shadow-lg transition-transform hover:scale-105",
          isUser
            ? "bg-gradient-to-br from-violet-500 to-purple-600 text-white shadow-violet-500/25"
            : "bg-gradient-to-br from-slate-700 to-slate-900 dark:from-slate-600 dark:to-slate-800 text-white shadow-slate-500/20"
        )}
      >
        {isUser ? <User className="h-5 w-5" /> : <Bot className="h-5 w-5" />}
      </motion.div>

      {/* Content */}
      <div
        className={cn(
          "flex flex-col gap-1",
          isUser ? "max-w-[85%] items-end" : "max-w-[85%] items-start"
        )}
      >
        <span className="text-[11px] font-medium text-slate-500 dark:text-slate-400 ml-1 mb-1">
          {isUser
            ? t("assistant.you", "You")
            : t("assistant.assistant", "Assistant")}
        </span>

        <div
          className={cn(
            "relative px-5 py-4 text-sm shadow-sm",
            isUser
              ? "bg-gradient-to-br from-violet-500 to-purple-600 text-white rounded-3xl rounded-tr-lg shadow-violet-500/20"
              : "bg-white dark:bg-slate-800/80 border border-slate-200/80 dark:border-slate-700/50 rounded-3xl rounded-tl-lg shadow-sm"
          )}
        >
          {/* Attachments for user messages */}
          {isUser && <AttachmentsDisplay attachments={message.attachments} />}

          {/* Search status display for assistant (GPT-like "Searching..." indicator) */}
          {!isUser && message.searchStatus && message.searchStatus.length > 0 && (
            <SearchStatusDisplay searchStatus={message.searchStatus} />
          )}

          {/* Web search results for assistant */}
          {!isUser && message.webSearchResults && message.webSearchResults.length > 0 && (
            <WebSearchDisplay results={message.webSearchResults} />
          )}

          {/* Context display for assistant messages */}
          {!isUser && message.contexts && message.contexts.length > 0 && (
            <ContextDisplay contexts={message.contexts} />
          )}

          {/* Message content */}
          {isUser ? (
            <div className="whitespace-pre-wrap leading-relaxed">
              {message.content}
            </div>
          ) : (
            <div className="text-slate-700 dark:text-slate-200">
              {/* GPT-style image generation placeholder */}
              {message.isGeneratingImage ? (
                <ImageGeneratingPlaceholder prompt={message.imageGenerationPrompt} />
              ) : message.isStreaming ? (
                message.content ? (
                  <StreamOutput
                    text={message.content}
                    isStreaming={true}
                    id={`msg-${message.id}`}
                  />
                ) : (
                  <div className="flex items-center gap-3 text-slate-500 dark:text-slate-400">
                    <div className="flex gap-1">
                      <motion.div
                        className="w-2 h-2 rounded-full bg-violet-500"
                        animate={{ scale: [1, 1.2, 1] }}
                        transition={{ duration: 0.6, repeat: Infinity, delay: 0 }}
                      />
                      <motion.div
                        className="w-2 h-2 rounded-full bg-purple-500"
                        animate={{ scale: [1, 1.2, 1] }}
                        transition={{ duration: 0.6, repeat: Infinity, delay: 0.2 }}
                      />
                      <motion.div
                        className="w-2 h-2 rounded-full bg-fuchsia-500"
                        animate={{ scale: [1, 1.2, 1] }}
                        transition={{ duration: 0.6, repeat: Infinity, delay: 0.4 }}
                      />
                    </div>
                    <span className="text-sm">{t("assistant.thinking", "Thinking...")}</span>
                  </div>
                )
              ) : message.content ? (
                <StreamOutput
                  text={message.content}
                  isStreaming={false}
                  id={`msg-${message.id}`}
                />
              ) : (
                <span className="text-slate-400 italic">
                  {t("assistant.emptyResponse", "(No response)")}
                </span>
              )}
            </div>
          )}

          {/* Phase 3: Citation display for assistant messages */}
          {!isUser && !message.isStreaming && message.ragCitations && message.ragCitations.length > 0 && (
            <CitationDisplay
              citations={message.ragCitations}
              evaluation={message.ragEvaluation}
            />
          )}
        </div>

        {/* Stats for assistant messages */}
        {!isUser && !message.isStreaming && <StatsBadge message={message} />}
      </div>
    </motion.div>
  );
}
