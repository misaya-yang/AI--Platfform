/**
 * TimelineStep — Claude-design reference
 *
 * Each step is a compact row. Status marker on the left (✓ completed,
 * ○ pending/running, ⊗ error). Content to its right.
 *
 * - Narrative / thinking step: bold title + small prose body, no rail icon.
 * - Tool step: monospace tool name (e.g. `web_search`) inline with a
 *   topical glyph (Search/Globe/File/...); query arg rendered in a gray
 *   code-block; source chips as small dot-prefixed pills.
 *
 * The previous colored-bubble icons on a left rail were too loud. This
 * version mirrors the reference (ChatGPT / Claude activity panel).
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Globe,
  Search,
  FileText,
  FilePlus,
  Terminal,
  Image as ImageIcon,
  FileOutput,
  Check,
  Circle,
  AlertCircle,
  Loader2,
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
      /** Original tool name — used for the monospace label (e.g. `web_search`). */
      toolName?: string;
      /** Original query / URL / path — rendered as a code block under the name. */
      queryArg?: string;
    };

// Topical glyph shown inline next to the monospace tool name.
const TOOL_GLYPH: Record<TimelineIcon, React.ComponentType<{ className?: string }>> = {
  thinking: Circle,
  web: Search,
  file_read: FileText,
  file_write: FilePlus,
  code: Terminal,
  image: ImageIcon,
  document: FileOutput,
  other: Globe,
};

// ---------------------------------------------------------------------------
// Status marker (left-side ✓ / ○ / ⊗)
// ---------------------------------------------------------------------------

function StatusMarker({
  status,
  kind,
}: {
  status?: "running" | "completed" | "error";
  kind: "thinking" | "tool";
}) {
  const effective = kind === "thinking" ? "completed" : status ?? "running";
  if (effective === "completed") {
    return (
      <div className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-slate-300 bg-white text-slate-500 dark:border-slate-600 dark:bg-slate-900 dark:text-slate-400">
        <Check className="h-2.5 w-2.5" strokeWidth={2.5} />
      </div>
    );
  }
  if (effective === "error") {
    return (
      <div className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-red-400 bg-red-50 text-red-500 dark:border-red-600/60 dark:bg-red-950/40">
        <AlertCircle className="h-2.5 w-2.5" />
      </div>
    );
  }
  return (
    <div className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full border border-slate-300 dark:border-slate-600">
      <Loader2 className="h-2.5 w-2.5 animate-spin text-slate-400" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// SourceChip — "• domain.com"
// ---------------------------------------------------------------------------

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
      className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[11px] text-slate-600 hover:border-slate-300 hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300 dark:hover:border-slate-600 dark:hover:bg-slate-800/80 transition-colors max-w-[200px]"
    >
      {failed ? (
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-slate-400" />
      ) : (
        <img
          src={favicon}
          alt=""
          loading="lazy"
          width={10}
          height={10}
          className="h-2.5 w-2.5 shrink-0 rounded-sm"
          onError={() => setFailed(true)}
        />
      )}
      <span className="truncate">{source.domain}</span>
    </a>
  );
}

// ---------------------------------------------------------------------------

function formatDuration(ms?: number): string | null {
  if (ms == null || !Number.isFinite(ms) || ms <= 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

interface TimelineStepProps {
  step: TimelineStepData;
  isLast?: boolean;
}

export function TimelineStep({ step }: TimelineStepProps) {
  const { t } = useTranslation();
  const status = step.kind === "tool" ? step.status : undefined;
  const duration = step.kind === "tool" ? formatDuration(step.durationMs) : null;
  const sources = step.kind === "tool" ? step.sources : undefined;
  const Glyph = step.kind === "tool" ? TOOL_GLYPH[step.icon] ?? Globe : null;
  const toolName = step.kind === "tool" ? step.toolName : undefined;
  const queryArg = step.kind === "tool" ? step.queryArg : undefined;

  return (
    <div className="flex gap-3 py-2.5">
      <div className="pt-[3px]">
        <StatusMarker status={status} kind={step.kind} />
      </div>
      <div className="min-w-0 flex-1">
        {step.kind === "thinking" ? (
          <>
            <div className="text-[13px] font-semibold text-slate-900 dark:text-slate-100">
              {step.title}
            </div>
            {step.body && (
              <p className="mt-1 whitespace-pre-wrap text-[12.5px] leading-relaxed text-slate-600 dark:text-slate-400">
                {step.body}
              </p>
            )}
          </>
        ) : (
          <>
            {/* Tool name row — monospace like `web_search` + glyph + duration */}
            <div className="flex items-center gap-2">
              {Glyph && <Glyph className="h-3.5 w-3.5 shrink-0 text-slate-500 dark:text-slate-400" />}
              <span className="font-mono text-[12.5px] font-medium text-slate-900 dark:text-slate-100">
                {toolName || step.title}
              </span>
              {duration && (
                <span className="ml-auto shrink-0 font-mono text-[11px] text-slate-400 dark:text-slate-500">
                  {duration}
                </span>
              )}
            </div>
            {/* Query / URL / path as a gray code-block if present */}
            {queryArg && (
              <div
                className={cn(
                  "mt-1.5 rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1",
                  "dark:border-slate-700/60 dark:bg-slate-900/40",
                )}
              >
                <code className="block truncate font-mono text-[12px] text-slate-700 dark:text-slate-300">
                  {queryArg}
                </code>
              </div>
            )}
            {/* Fallback body text (for tools without a query arg, e.g. code exec) */}
            {!queryArg && step.body && (
              <p className="mt-1 line-clamp-3 whitespace-pre-wrap text-[12px] leading-relaxed text-slate-500 dark:text-slate-400">
                {step.body}
              </p>
            )}
            {/* Source chips */}
            {sources && sources.length > 0 && (
              <div className="mt-1.5 flex flex-wrap gap-1">
                {sources.map((src, idx) => (
                  <SourceChip key={`${src.url}-${idx}`} source={src} />
                ))}
              </div>
            )}
            {status === "error" && !step.body && (
              <p className="mt-1 text-[11.5px] text-red-500">
                {t("playground.activity.errorFallback", "Step failed")}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  );
}
