/**
 * TimelineStep
 *
 * Single entry in the activity timeline — icon on the rail, title, body, and
 * optional source chips. Designed to match Claude.ai's reasoning-trace layout.
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Brain,
  Globe,
  FileText,
  FilePlus,
  Terminal,
  Image as ImageIcon,
  FileOutput,
  Circle,
  Loader2,
  AlertCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";

export type TimelineIcon =
  | "thinking"
  | "web"
  | "file_read"
  | "file_write"
  | "code"
  | "image"
  | "document"
  | "other";

export interface TimelineSource {
  domain: string;
  url: string;
  title?: string;
}

export type TimelineStepData =
  | {
      kind: "thinking";
      id: string;
      title: string;
      body: string;
      startedAt?: number;
    }
  | {
      kind: "tool";
      id: string;
      icon: TimelineIcon;
      title: string;
      body: string;
      sources?: TimelineSource[];
      durationMs?: number;
      status: "running" | "completed" | "error";
    };

const ICON_STYLE: Record<
  TimelineIcon,
  { Icon: React.ComponentType<{ className?: string }>; bg: string; fg: string }
> = {
  thinking: {
    Icon: Brain,
    bg: "bg-slate-200 dark:bg-slate-700",
    fg: "text-slate-600 dark:text-slate-300",
  },
  web: {
    Icon: Globe,
    bg: "bg-blue-100 dark:bg-blue-900/50",
    fg: "text-blue-600 dark:text-blue-300",
  },
  file_read: {
    Icon: FileText,
    bg: "bg-amber-100 dark:bg-amber-900/50",
    fg: "text-amber-600 dark:text-amber-300",
  },
  file_write: {
    Icon: FilePlus,
    bg: "bg-amber-100 dark:bg-amber-900/50",
    fg: "text-amber-700 dark:text-amber-200",
  },
  code: {
    Icon: Terminal,
    bg: "bg-violet-100 dark:bg-violet-900/50",
    fg: "text-violet-600 dark:text-violet-300",
  },
  image: {
    Icon: ImageIcon,
    bg: "bg-pink-100 dark:bg-pink-900/50",
    fg: "text-pink-600 dark:text-pink-300",
  },
  document: {
    Icon: FileOutput,
    bg: "bg-emerald-100 dark:bg-emerald-900/50",
    fg: "text-emerald-600 dark:text-emerald-300",
  },
  other: {
    Icon: Circle,
    bg: "bg-slate-200 dark:bg-slate-700",
    fg: "text-slate-600 dark:text-slate-300",
  },
};

function SourceChip({ source }: { source: TimelineSource }) {
  const [failed, setFailed] = useState(false);
  const favicon = `https://www.google.com/s2/favicons?domain=${encodeURIComponent(
    source.domain,
  )}&sz=32`;

  return (
    <a
      href={source.url}
      target="_blank"
      rel="noopener noreferrer"
      title={source.title || source.url}
      className="inline-flex items-center gap-1.5 rounded-full bg-slate-100 dark:bg-slate-800 px-2 py-0.5 text-[11px] text-slate-600 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors max-w-[220px]"
    >
      {failed ? (
        <Globe className="h-3 w-3 shrink-0 text-slate-400" />
      ) : (
        <img
          src={favicon}
          alt=""
          loading="lazy"
          width={12}
          height={12}
          className="h-3 w-3 shrink-0 rounded-sm"
          onError={() => setFailed(true)}
        />
      )}
      <span className="truncate">{source.domain}</span>
    </a>
  );
}

function formatDuration(ms?: number): string | null {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

interface TimelineStepProps {
  step: TimelineStepData;
  isLast?: boolean;
}

export function TimelineStep({ step, isLast = false }: TimelineStepProps) {
  const { t } = useTranslation();

  const iconKey: TimelineIcon = step.kind === "thinking" ? "thinking" : step.icon;
  const style = ICON_STYLE[iconKey] ?? ICON_STYLE.other;
  const { Icon } = style;

  const status = step.kind === "tool" ? step.status : undefined;
  const duration = step.kind === "tool" ? formatDuration(step.durationMs) : null;
  const sources = step.kind === "tool" ? step.sources : undefined;

  return (
    <div className={cn("relative pl-6", !isLast && "pb-5")}>
      {/* Icon bubble on the rail */}
      <div
        className={cn(
          "absolute -left-[9px] top-0.5 flex h-[18px] w-[18px] items-center justify-center rounded-full ring-2 ring-[hsl(var(--assistant-surface))] dark:ring-[hsl(var(--assistant-surface))]",
          style.bg,
        )}
        aria-hidden
      >
        {status === "running" ? (
          <Loader2 className={cn("h-3 w-3 animate-spin", style.fg)} />
        ) : status === "error" ? (
          <AlertCircle className="h-3 w-3 text-red-500" />
        ) : (
          <Icon className={cn("h-[11px] w-[11px]", style.fg)} />
        )}
      </div>

      {/* Title row */}
      <div className="flex items-baseline gap-2">
        <h4 className="text-[14px] font-medium text-slate-900 dark:text-slate-100 leading-snug">
          {step.title}
        </h4>
        {duration && (
          <span className="shrink-0 font-mono text-[11px] text-slate-400 dark:text-slate-500">
            {duration}
          </span>
        )}
      </div>

      {/* Body (reasoning / description) */}
      {step.body && (
        <p className="mt-1 whitespace-pre-wrap text-[13px] leading-relaxed text-slate-600 dark:text-slate-400">
          {step.body}
        </p>
      )}

      {/* Source chips */}
      {sources && sources.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {sources.map((src, idx) => (
            <SourceChip key={`${src.url}-${idx}`} source={src} />
          ))}
        </div>
      )}

      {status === "error" && step.kind === "tool" && !step.body && (
        <p className="mt-1 text-[12px] text-red-500">
          {t("playground.activity.errorFallback", "Step failed")}
        </p>
      )}
    </div>
  );
}
