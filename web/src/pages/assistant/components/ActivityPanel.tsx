/**
 * ActivityPanel
 *
 * Inline expandable "activity timeline" that sits right below the trigger
 * chip inside the message flow. Claude.ai shows a side panel for this, but
 * we already occupy the right rail with the artifacts panel — stacking two
 * right-side sheets would compete for the same real estate. Inline keeps
 * the feature visible without that conflict and without adding modality.
 *
 * Closed: nothing rendered (the chip in ChatMessage is the only visible
 * affordance).
 * Open: a bordered card expands downward with animated height, showing the
 * activity header + timeline body + an × button. Close button collapses
 * back to just the chip.
 */

import { useTranslation } from "react-i18next";
import { AnimatePresence, motion } from "framer-motion";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import type { ChatMessage as ChatMessageType } from "../types";
import { ActivityTimeline } from "./ActivityTimeline";

interface ActivityPanelProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  message: ChatMessageType;
  totalDurationMs?: number;
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
}: ActivityPanelProps) {
  const { t } = useTranslation();
  const durationLabel = formatTotal(totalDurationMs);
  const title = durationLabel
    ? t("playground.activity.titleWithDuration", {
        duration: durationLabel,
        defaultValue: "Activity · {{duration}}",
      })
    : t("playground.activity.title", { defaultValue: "Activity" });

  return (
    <AnimatePresence initial={false}>
      {open && (
        <motion.div
          key="activity-panel"
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={{ duration: 0.22, ease: [0.32, 0.72, 0, 1] }}
          className="overflow-hidden"
        >
          <div
            className={cn(
              "mt-2 rounded-xl border border-slate-200 dark:border-slate-800",
              "bg-slate-50/50 dark:bg-slate-900/40",
            )}
          >
            {/* Header */}
            <div className="flex items-center justify-between border-b border-slate-200 dark:border-slate-800 px-4 py-2.5">
              <div className="text-[13px] font-semibold tracking-tight text-slate-700 dark:text-slate-200">
                {title}
              </div>
              <button
                type="button"
                onClick={() => onOpenChange(false)}
                className="rounded-md p-1 text-slate-500 hover:bg-slate-200/60 hover:text-slate-900 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-100 transition-colors"
                aria-label={t("common.close", { defaultValue: "Close" })}
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>

            {/* Timeline body — caps height so long traces scroll locally
                rather than pushing the message below off-screen. */}
            <div className="max-h-[480px] overflow-y-auto px-4 py-4">
              <ActivityTimeline message={message} />
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
