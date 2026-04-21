/**
 * ActivityPanel — inline, Claude-design reference
 *
 * Collapsed: nothing rendered.
 * Open: a compact card (~ 420px) with:
 *   - Activity icon + "Activity" + status · steps · duration + close
 *   - Timeline list (TimelineStep per row)
 *
 * We keep the panel inline because the right rail is occupied by the
 * artifacts panel; a second right-side sheet would compete for space.
 */

import { useTranslation } from "react-i18next";
import { AnimatePresence, motion } from "framer-motion";
import { Sparkles, X } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ChatMessage as ChatMessageType } from "../types";
import { ActivityTimeline } from "./ActivityTimeline";

interface ActivityPanelProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  message: ChatMessageType;
  totalDurationMs?: number;
  stepCount?: number;
  isStreaming?: boolean;
}

function formatTotal(ms?: number): string {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m ${s}s`;
}

export function ActivityPanel({
  open,
  onOpenChange,
  message,
  totalDurationMs,
  stepCount,
  isStreaming,
}: ActivityPanelProps) {
  const { t } = useTranslation();
  const durationLabel = formatTotal(totalDurationMs);
  const statusWord = isStreaming
    ? t("playground.activity.running", { defaultValue: "Running…" })
    : t("playground.activity.statusCompleted", { defaultValue: "completed" });

  // Header subtitle like: "completed · 6 steps · 50.2s"
  const subtitleParts: string[] = [statusWord];
  if (typeof stepCount === "number") {
    subtitleParts.push(
      t("playground.activity.steps", {
        count: stepCount,
        defaultValue: "{{count}} steps",
      }),
    );
  }
  if (durationLabel) subtitleParts.push(durationLabel);
  const subtitle = subtitleParts.join(" · ");

  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          key="activity-panel"
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={{ duration: 0.2, ease: [0.32, 0.72, 0, 1] }}
          className="overflow-hidden"
        >
          <div
            className={cn(
              "mt-2 max-w-[440px] rounded-xl border shadow-sm",
              "border-slate-200 bg-white",
              "dark:border-slate-800 dark:bg-slate-950/80",
            )}
          >
            {/* Header */}
            <div className="flex items-start justify-between gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-800">
              <div className="flex items-start gap-2.5">
                <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-indigo-100 text-indigo-600 dark:bg-indigo-950/50 dark:text-indigo-400">
                  <Sparkles className="h-3.5 w-3.5" />
                </div>
                <div className="leading-tight">
                  <div className="text-[13px] font-semibold text-slate-900 dark:text-slate-100">
                    {t("playground.activity.title", { defaultValue: "Activity" })}
                  </div>
                  <div className="mt-0.5 text-[11.5px] text-slate-500 dark:text-slate-400">
                    {subtitle}
                  </div>
                </div>
              </div>
              <button
                type="button"
                onClick={() => onOpenChange(false)}
                className="rounded-md p-1 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-600 dark:hover:bg-slate-800 dark:hover:text-slate-200"
                aria-label={t("common.close", { defaultValue: "Close" })}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>

            {/* Timeline body — scroll locally so long traces don't shove
                the answer off-screen. */}
            <div className="max-h-[520px] overflow-y-auto px-3.5 py-2">
              <ActivityTimeline message={message} />
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
