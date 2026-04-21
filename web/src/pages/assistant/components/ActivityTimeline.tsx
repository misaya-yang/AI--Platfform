/**
 * ActivityTimeline
 *
 * Renders a list of TimelineStep rows on a continuous left rail. The
 * ChatMessage → steps transformation lives in ./buildTimeline.ts.
 */

import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";
import type { ChatMessage as ChatMessageType } from "../types";
import { TimelineStep } from "./TimelineStep";
import { buildTimeline } from "./buildTimeline";

interface ActivityTimelineProps {
  message: ChatMessageType;
  className?: string;
}

export function ActivityTimeline({ message, className }: ActivityTimelineProps) {
  const { t } = useTranslation();
  const { steps } = useMemo(() => buildTimeline(message, t), [message, t]);

  if (steps.length === 0) {
    return (
      <div className={className}>
        <p className="text-sm text-slate-500 dark:text-slate-400">
          {t("playground.activity.empty", { defaultValue: "No activity recorded." })}
        </p>
      </div>
    );
  }

  return (
    <div className={cn("divide-y divide-slate-100 dark:divide-slate-800/60", className)}>
      {steps.map((step, idx) => (
        <TimelineStep key={step.id} step={step} isLast={idx === steps.length - 1} />
      ))}
    </div>
  );
}
