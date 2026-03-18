import { motion } from "framer-motion";
import { useTranslation } from "react-i18next";
import { Clock, Zap, MessageSquare } from "lucide-react";
import type { ChatMessage } from "@/components/ChatWindow";

export function StatsBadge({ stats }: { stats: NonNullable<ChatMessage["stats"]> }) {
  const { t } = useTranslation();

  const hasStats = stats.durationMs != null || stats.firstTokenMs != null || stats.totalTokens != null;
  if (!hasStats) return null;

  return (
    <motion.div
      data-message-stats="true"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.3, duration: 0.4, ease: "easeOut" }}
      className="flex flex-wrap items-center gap-2 text-[10px] mt-3 ml-1"
    >
      {/* Duration - with subtle gradient */}
      {stats.durationMs != null && (
        <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-gradient-to-r from-slate-100 to-slate-50 dark:from-zinc-800/80 dark:to-zinc-800/50 text-slate-600 dark:text-zinc-400 border border-slate-200/50 dark:border-zinc-700/50 backdrop-blur-sm">
          <Clock className="h-3 w-3 text-slate-500 dark:text-zinc-500" />
          <span className="font-medium">{(stats.durationMs / 1000).toFixed(2)}s</span>
        </span>
      )}

      {/* TTFT - accent color */}
      {stats.firstTokenMs != null && (
        <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-gradient-to-r from-blue-500/10 to-indigo-500/10 text-blue-600 dark:text-blue-400 border border-blue-200/50 dark:border-blue-500/20 backdrop-blur-sm">
          <Zap className="h-3 w-3" />
          <span className="font-medium">{t("playground.stats.ttft", "TTFT")}: {stats.firstTokenMs}ms</span>
        </span>
      )}

      {/* Token count - with detail tooltip */}
      {stats.totalTokens != null && (
        <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-full bg-gradient-to-r from-slate-100 to-slate-50 dark:from-zinc-800/80 dark:to-zinc-800/50 text-slate-600 dark:text-zinc-400 border border-slate-200/50 dark:border-zinc-700/50 backdrop-blur-sm">
          <MessageSquare className="h-3 w-3 text-slate-500 dark:text-zinc-500" />
          <span className="font-medium">{stats.totalTokens}</span>
          <span className="text-slate-400 dark:text-zinc-500">{t("playground.stats.tokens", "tokens")}</span>
          {stats.inputTokens != null && stats.outputTokens != null && (
            <span className="text-slate-400 dark:text-zinc-500 text-[9px]">
              ({stats.inputTokens}↓ {stats.outputTokens}↑)
            </span>
          )}
        </span>
      )}
    </motion.div>
  );
}
