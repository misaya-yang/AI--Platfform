import { motion } from "framer-motion";
import { useTranslation } from "react-i18next";
import { Clock, Zap, MessageSquare } from "lucide-react";
import type { ChatMessage } from "@/components/ChatWindow";

export function StatsBadge({
  stats,
}: {
  stats: NonNullable<ChatMessage["stats"]>;
}) {
  const { t } = useTranslation();

  const hasStats =
    stats.durationMs != null ||
    stats.firstTokenMs != null ||
    stats.totalTokens != null;
  if (!hasStats) return null;

  return (
    <motion.div
      data-message-stats="true"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.2, duration: 0.35, ease: "easeOut" }}
      className="flex flex-wrap items-center gap-1.5 text-[10px] mt-2.5 ml-1"
    >
      {/* Duration */}
      {stats.durationMs != null && (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-slate-100/80 dark:bg-zinc-800/60 text-slate-500 dark:text-zinc-400 border border-slate-200/40 dark:border-zinc-700/40">
          <Clock className="h-2.5 w-2.5" />
          <span className="font-medium tabular-nums">
            {(stats.durationMs / 1000).toFixed(2)}s
          </span>
        </span>
      )}

      {/* TTFT */}
      {stats.firstTokenMs != null && (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-blue-50/80 dark:bg-blue-950/30 text-blue-600 dark:text-blue-400 border border-blue-200/40 dark:border-blue-800/30">
          <Zap className="h-2.5 w-2.5" />
          <span className="font-medium tabular-nums">
            {t("playground.stats.ttft", "TTFT")}: {stats.firstTokenMs}ms
          </span>
        </span>
      )}

      {/* Token count */}
      {stats.totalTokens != null && (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-slate-100/80 dark:bg-zinc-800/60 text-slate-500 dark:text-zinc-400 border border-slate-200/40 dark:border-zinc-700/40">
          <MessageSquare className="h-2.5 w-2.5" />
          <span className="font-medium tabular-nums">{stats.totalTokens}</span>
          <span className="text-slate-400 dark:text-zinc-500">
            {t("playground.stats.tokens", "tokens")}
          </span>
          {stats.inputTokens != null && stats.outputTokens != null && (
            <span className="text-slate-400 dark:text-zinc-500 text-[9px] tabular-nums">
              ({stats.inputTokens}/{stats.outputTokens})
            </span>
          )}
        </span>
      )}
    </motion.div>
  );
}
