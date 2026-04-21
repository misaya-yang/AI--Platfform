/**
 * ArtifactsPanel — right-side "generated files" drawer.
 *
 * Visual contract (aligned with ActivityPanel.tsx):
 *  - Panel background: `--assistant-canvas-bg` (same plane as the chat).
 *  - Single 1px hairline on the left in `--assistant-border`, no cards.
 *  - Header: small monochrome glyph + 13px semibold title + 11px mono subtitle,
 *    close button with the shared `act-btn act-hover` treatment (26x26, r=6).
 *  - Tabs: text + gold underline indicator when active. Inactive tabs
 *    have no background.
 *  - Artifact cells: `--assistant-surface-bg` + `--assistant-border`. No
 *    violet focus rings, no gradient format badges — a small muted format
 *    glyph instead.
 *  - All action buttons (copy, download, rerun, open): `act-btn act-hover`,
 *    6px radius, ghost at rest.
 *
 * We deliberately DO NOT restore a floating Artifacts modal. The panel's
 * multi-file + multi-view (preview / code / output) scale poorly as a
 * floating popup, and the right-side drawer keeps parity with Activity.
 * If the user asks for "悬浮窗" again, re-read this block first.
 *
 * Image loading note: S3 presigned URLs load directly; for the fallback
 * `/api/v1/assistant/artifacts/<id>/download` route (used when storage
 * can't produce a public URL) the browser can't attach the Bearer token
 * on `<img src>`. We therefore fetch via axios and render a blob: URL.
 */

import * as React from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  X,
  Copy,
  Check,
  Download,
  Code2,
  Eye,
  FileText,
  Loader2,
  Play,
  Terminal,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { toast } from "@/hooks/use-toast";
import { formatFileSize, getFormatLabel } from "@/lib/format";
import type { ExecutionStatusType } from "./ExecutionStatus";
import { copyToClipboard } from "@/lib/clipboard";
import { useTranslation } from "react-i18next";
import { api } from "@/lib/api";

// ============================================================================
// Types
// ============================================================================

export interface ArtifactVersion {
  id: string;
  version: number;
  createdAt: Date;
  preview?: string;
}

export interface Artifact {
  id: string;
  type: "code" | "chart" | "table" | "file" | "image" | "document";
  format: string;
  title: string;
  url?: string;
  content?: string;
  createdAt: Date;
  filename?: string;
  mimeType?: string;
  sizeBytes?: number;
  source?: "ai" | "user" | "code_execution" | "image_generation" | "document_generation";
  versions?: ArtifactVersion[];
  currentVersion?: number;
}

export interface OutputFile {
  filename: string;
  content_base64: string;
  mime_type: string | null;
  size_bytes: number;
  artifact_id?: string;
  download_url?: string;
}

interface ArtifactsPanelProps {
  isOpen: boolean;
  onClose: () => void;
  artifacts: Artifact[];
  executionStatus: ExecutionStatusType;
  executionOutput: string;
  currentCode?: string;
  executionTimeMs?: number;
  outputFiles?: OutputFile[];
  onRerun?: () => void;
  className?: string;
  isExpanded?: boolean;
  onToggleExpand?: () => void;
  onRefine?: (artifact: Artifact) => void;
  onVersionSelect?: (artifactId: string, versionId: string) => void;
}

const ASSISTANT_UI_V2 = import.meta.env.VITE_ASSISTANT_UI_V2 !== "false";

// ============================================================================
// Helpers — auth-aware image loading
// ============================================================================

function needsAuthenticatedFetch(src: string | undefined): boolean {
  if (!src) return false;
  if (/^https?:\/\//i.test(src)) return false;
  if (src.startsWith("data:") || src.startsWith("blob:")) return false;
  return src.startsWith("/api/") || src.includes("/api/v1/assistant/artifacts/");
}

async function fetchAsBlobUrl(src: string): Promise<string> {
  const res = await api.get(src, { responseType: "blob" });
  return URL.createObjectURL(res.data as Blob);
}

// ============================================================================
// Sub-components
// ============================================================================

/** Status indicator — single accent (gold) only; destructive uses the global token. */
function StatusIndicator({ status, timeMs }: { status: ExecutionStatusType; timeMs?: number }) {
  if (status === "idle") return null;

  const running = status === "running";
  const ok = status === "success";
  const bad = status === "error" || status === "timeout";

  return (
    <div className="flex items-center gap-1.5 text-[11px] font-mono text-[hsl(var(--assistant-text-tertiary))]">
      {running && (
        <>
          <Loader2 className="h-3 w-3 animate-spin text-[hsl(var(--assistant-accent))]" />
          <span>Running</span>
        </>
      )}
      {ok && (
        <>
          <Check className="h-3 w-3 text-[hsl(var(--assistant-accent))]" />
          <span>
            Ready{timeMs ? ` · ${(timeMs / 1000).toFixed(1)}s` : ""}
          </span>
        </>
      )}
      {bad && (
        <>
          <X className="h-3 w-3 text-[hsl(var(--destructive))]" />
          <span>{status === "timeout" ? "Timeout" : "Error"}</span>
        </>
      )}
    </div>
  );
}

/** Tabs — text + gold underline when active. No pill background. */
function ViewToggle({
  view,
  onChange,
  hasCode,
}: {
  view: "preview" | "code" | "output";
  onChange: (v: "preview" | "code" | "output") => void;
  hasCode: boolean;
}) {
  const tabs: Array<{ id: typeof view; label: string; icon: React.ReactNode }> = [
    { id: "preview", label: "Preview", icon: <Eye className="h-3.5 w-3.5" /> },
    ...(hasCode ? [{ id: "code" as const, label: "Code", icon: <Code2 className="h-3.5 w-3.5" /> }] : []),
    { id: "output", label: "Output", icon: <Terminal className="h-3.5 w-3.5" /> },
  ];

  return (
    <div className="inline-flex items-center gap-1">
      {tabs.map((tab) => {
        const active = view === tab.id;
        return (
          <button
            key={tab.id}
            type="button"
            onClick={() => onChange(tab.id)}
            className={cn(
              "relative flex items-center gap-1.5 px-2.5 py-1.5 text-[12.5px] font-medium rounded-md transition-colors",
              active
                ? "text-[hsl(var(--assistant-text-primary))]"
                : "text-[hsl(var(--assistant-text-tertiary))] hover:text-[hsl(var(--assistant-text-secondary))]",
            )}
          >
            <span className={active ? "text-[hsl(var(--assistant-accent))]" : undefined}>
              {tab.icon}
            </span>
            <span>{tab.label}</span>
            {/* Active underline — 1.5px gold stripe */}
            {active && (
              <span
                aria-hidden
                className="absolute left-2.5 right-2.5 -bottom-[5px] h-[1.5px] rounded-sm bg-[hsl(var(--assistant-accent))]"
              />
            )}
          </button>
        );
      })}
    </div>
  );
}

/**
 * Small muted format glyph (replaces the loud colored square). Shows
 * the first 3-4 chars of the format in mono at text-tertiary.
 */
function FormatGlyph({ label }: { label: string }) {
  return (
    <div
      className={cn(
        "flex items-center justify-center w-9 h-9 rounded-md",
        "bg-[hsl(var(--assistant-chip-bg))] border border-[hsl(var(--assistant-border))]",
        "text-[9px] font-mono font-semibold tracking-wider uppercase",
        "text-[hsl(var(--assistant-text-tertiary))]",
      )}
    >
      {label.slice(0, 4)}
    </div>
  );
}

/** Single artifact row — ghost card, hairline, no shadow at rest. */
function ArtifactCard({
  artifact,
  onDownload,
}: {
  artifact: Artifact | (OutputFile & { title?: string });
  onDownload: () => void;
}) {
  const { t } = useTranslation();
  const isOutputFile = "content_base64" in artifact;
  const title = isOutputFile
    ? (artifact as OutputFile).filename
    : (artifact as Artifact).title || (artifact as Artifact).filename;
  const rawFormat = isOutputFile ? undefined : (artifact as Artifact).format;
  const mimeType = isOutputFile
    ? (artifact as OutputFile).mime_type
    : (artifact as Artifact).mimeType;
  const formatLabel = getFormatLabel(rawFormat, mimeType || undefined);
  const size = isOutputFile
    ? (artifact as OutputFile).size_bytes
    : (artifact as Artifact).sizeBytes;

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "group flex items-center gap-3 p-3 rounded-lg transition-colors",
        "bg-[hsl(var(--assistant-surface-bg))] border border-[hsl(var(--assistant-border))]",
        "hover:bg-[hsl(var(--assistant-surface-soft))]",
      )}
    >
      <FormatGlyph label={formatLabel} />
      <div className="flex-1 min-w-0">
        <p className="text-[13px] font-medium text-[hsl(var(--assistant-text-primary))] truncate">
          {title}
        </p>
        <p className="text-[11px] font-mono text-[hsl(var(--assistant-text-tertiary))]">
          {formatLabel}
          {size ? ` · ${formatFileSize(size)}` : ""}
        </p>
      </div>
      <button
        type="button"
        onClick={onDownload}
        className={cn(
          "act-btn act-hover inline-flex items-center gap-1.5 px-2 py-1.5 rounded-md",
          "text-[11.5px] text-[hsl(var(--assistant-text-secondary))]",
          "hover:text-[hsl(var(--assistant-text-primary))]",
          "opacity-0 group-hover:opacity-100 focus-visible:opacity-100",
          "border-0 bg-transparent transition-opacity",
        )}
        aria-label={t("artifact.download", "Download")}
      >
        <Download className="h-3.5 w-3.5" />
        <span>{t("artifact.download", "Download")}</span>
      </button>
    </motion.div>
  );
}

/** Image cell with auth-aware blob fetch for same-origin API routes. */
function ImageCard({
  item,
  onDownload,
}: {
  item: OutputFile | Artifact;
  onDownload: () => void;
}) {
  const { t } = useTranslation();
  const isOutputFile = "content_base64" in item;
  const rawSrc = isOutputFile
    ? `data:${item.mime_type || "image/png"};base64,${item.content_base64}`
    : (item as Artifact).url;
  const filename = isOutputFile
    ? item.filename
    : (item as Artifact).filename || (item as Artifact).title;
  const fileSize = isOutputFile ? item.size_bytes : (item as Artifact).sizeBytes;

  const [resolvedSrc, setResolvedSrc] = React.useState<string | undefined>(() =>
    rawSrc && !needsAuthenticatedFetch(rawSrc) ? rawSrc : undefined,
  );
  const [loadError, setLoadError] = React.useState(false);

  React.useEffect(() => {
    if (!rawSrc) {
      setResolvedSrc(undefined);
      return;
    }
    if (!needsAuthenticatedFetch(rawSrc)) {
      setResolvedSrc(rawSrc);
      setLoadError(false);
      return;
    }
    let cancelled = false;
    let objectUrl: string | null = null;
    setLoadError(false);
    fetchAsBlobUrl(rawSrc)
      .then((blobUrl) => {
        if (cancelled) {
          URL.revokeObjectURL(blobUrl);
          return;
        }
        objectUrl = blobUrl;
        setResolvedSrc(blobUrl);
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [rawSrc]);

  const handleOpenInNewTab = () => {
    const target = resolvedSrc || rawSrc;
    if (!target) return;
    const newWindow = window.open("", "_blank");
    if (newWindow) {
      const doc = newWindow.document;
      doc.title = filename || "Image";
      doc.body.style.cssText =
        "margin:0;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#1a1a1a;";
      const img = doc.createElement("img");
      img.src = target;
      img.alt = filename || "Image";
      img.style.cssText = "max-width:100%;max-height:100vh;object-fit:contain;";
      doc.body.appendChild(img);
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "group rounded-lg overflow-hidden transition-colors",
        "bg-[hsl(var(--assistant-surface-bg))] border border-[hsl(var(--assistant-border))]",
        "hover:bg-[hsl(var(--assistant-surface-soft))]",
      )}
    >
      <div className="relative aspect-video bg-[hsl(var(--assistant-chip-bg))] flex items-center justify-center">
        {resolvedSrc && !loadError ? (
          <img
            src={resolvedSrc}
            alt={filename || "Generated Image"}
            className="max-w-full max-h-full object-contain"
            onError={() => setLoadError(true)}
          />
        ) : loadError ? (
          <div className="flex flex-col items-center gap-1 text-[hsl(var(--assistant-text-tertiary))]">
            <FileText className="h-6 w-6 opacity-50" />
            <span className="text-[11px]">{t("common.imageLoadFailed", "Image failed to load")}</span>
          </div>
        ) : (
          <Loader2 className="h-5 w-5 animate-spin text-[hsl(var(--assistant-text-tertiary))]" />
        )}
        {resolvedSrc && !loadError && (
          <div className="absolute inset-0 flex items-center justify-center gap-2 opacity-0 group-hover:opacity-100 transition-opacity bg-black/25">
            <button
              type="button"
              onClick={handleOpenInNewTab}
              className="act-btn p-1.5 rounded-md bg-black/55 hover:bg-black/75 text-white backdrop-blur-sm"
              aria-label={t("common.openInNewTab", "Open in new tab")}
            >
              <Eye className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
      </div>

      <div className="flex items-center justify-between px-3 py-2 border-t border-[hsl(var(--assistant-border))]">
        <div className="min-w-0">
          <p className="text-[12px] font-medium text-[hsl(var(--assistant-text-primary))] truncate">
            {filename || "Image"}
          </p>
          {fileSize && (
            <p className="text-[10px] font-mono text-[hsl(var(--assistant-text-tertiary))]">
              {formatFileSize(fileSize)}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={onDownload}
          className={cn(
            "act-btn act-hover inline-flex items-center justify-center h-6 w-6 rounded-md",
            "text-[hsl(var(--assistant-text-secondary))] hover:text-[hsl(var(--assistant-text-primary))]",
            "border-0 bg-transparent",
          )}
          aria-label={t("artifact.download", "Download")}
        >
          <Download className="h-3 w-3" />
        </button>
      </div>
    </motion.div>
  );
}

// ============================================================================
// Main Component
// ============================================================================

export function ArtifactsPanel({
  isOpen,
  onClose,
  artifacts,
  executionStatus,
  executionOutput,
  currentCode,
  executionTimeMs,
  outputFiles = [],
  onRerun,
  className,
}: ArtifactsPanelProps) {
  const { t } = useTranslation();
  const [view, setView] = React.useState<"preview" | "code" | "output">("preview");
  const [copied, setCopied] = React.useState(false);

  const currentRunImages = React.useMemo(
    () => outputFiles.filter((f) => f.mime_type?.startsWith("image/")),
    [outputFiles],
  );
  const currentRunDocuments = React.useMemo(
    () => outputFiles.filter((f) => !f.mime_type?.startsWith("image/")),
    [outputFiles],
  );
  const sessionImages = React.useMemo(
    () => artifacts.filter((a) => a.type === "image" || a.mimeType?.startsWith("image/")),
    [artifacts],
  );
  const sessionDocuments = React.useMemo(
    () =>
      artifacts.filter(
        (a) =>
          a.type !== "image" &&
          !a.mimeType?.startsWith("image/") &&
          (a.type === "document" ||
            a.type === "file" ||
            ["docx", "pdf", "md", "xlsx", "csv"].includes(a.format)),
      ),
    [artifacts],
  );

  const imageFiles = React.useMemo<(Artifact | OutputFile)[]>(() => {
    const sessionIds = new Set(sessionImages.map((image) => image.id));
    const uniqueCurrentRunImages = currentRunImages.filter(
      (image) => !image.artifact_id || !sessionIds.has(image.artifact_id),
    );
    return [...sessionImages, ...uniqueCurrentRunImages];
  }, [sessionImages, currentRunImages]);

  const documentFiles = React.useMemo<(Artifact | OutputFile)[]>(() => {
    const sessionIds = new Set(sessionDocuments.map((doc) => doc.id));
    const uniqueCurrentRunDocs = currentRunDocuments.filter(
      (doc) => !doc.artifact_id || !sessionIds.has(doc.artifact_id),
    );
    return [...sessionDocuments, ...uniqueCurrentRunDocs];
  }, [sessionDocuments, currentRunDocuments]);

  const hasCode = Boolean(currentCode);

  const firstDoc = currentRunDocuments.at(0) ?? sessionDocuments.at(0);
  const displayTitle = firstDoc
    ? "content_base64" in firstDoc
      ? firstDoc.filename
      : firstDoc.title || firstDoc.filename
    : t("assistant.artifacts", "Artifacts");
  const displayFormat = firstDoc
    ? getFormatLabel(
        "format" in firstDoc ? (firstDoc as Artifact).format : undefined,
        "mime_type" in firstDoc
          ? (firstDoc as OutputFile).mime_type || undefined
          : (firstDoc as Artifact).mimeType || undefined,
      )
    : "";

  const totalCount =
    currentRunImages.length +
    currentRunDocuments.length +
    sessionImages.length +
    sessionDocuments.length;
  const subtitle = totalCount > 0
    ? `${totalCount} ${totalCount === 1 ? "file" : "files"}`
    : t("assistant.artifactsEmptySubtitle", "No files yet");

  const handleCopy = React.useCallback(async () => {
    const textToCopy = view === "code" ? currentCode : executionOutput;
    if (!textToCopy) return;
    try {
      await copyToClipboard(textToCopy);
      setCopied(true);
      toast.success("Copied to clipboard");
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("Failed to copy");
    }
  }, [view, currentCode, executionOutput]);

  const handleDownload = React.useCallback((item: Artifact | OutputFile) => {
    if ("download_url" in item && item.download_url) {
      window.open(item.download_url, "_blank");
      toast.success("Download started", (item as OutputFile).filename);
    } else if ("content_base64" in item) {
      const link = document.createElement("a");
      link.href = `data:${item.mime_type || "application/octet-stream"};base64,${item.content_base64}`;
      link.download = item.filename;
      link.click();
      toast.success("Download started", item.filename);
    } else if ("url" in item && item.url) {
      window.open(item.url, "_blank");
      toast.success("Download started", item.filename || item.title);
    }
  }, []);

  if (!isOpen) return null;

  return (
    <motion.div
      initial={{ opacity: 0, x: 20 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 20 }}
      transition={{ duration: 0.22, ease: [0.2, 0.8, 0.3, 1] }}
      className={cn(
        "flex flex-col h-full",
        "bg-[hsl(var(--assistant-canvas-bg))]",
        "border-l border-[hsl(var(--assistant-border))]",
        "font-assistant",
        className,
      )}
    >
      {/* Header — mirrors ActivityPanel: glyph + title + mono subtitle + close */}
      <div
        className={cn(
          "flex items-center gap-2.5 px-4 pt-[14px] pb-[10px] shrink-0",
          "border-b border-[hsl(var(--assistant-border-soft))]",
        )}
      >
        <span
          aria-hidden
          className="inline-flex items-center justify-center w-4 h-4 text-[hsl(var(--assistant-text-secondary))]"
        >
          <FileText className="h-3.5 w-3.5" />
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-[13px] font-semibold text-[hsl(var(--assistant-text-primary))] truncate">
              {displayTitle}
            </span>
            {displayFormat && (
              <span className="flex-shrink-0 px-1.5 py-[1px] text-[9px] font-mono font-semibold tracking-wider uppercase rounded bg-[hsl(var(--assistant-chip-bg))] text-[hsl(var(--assistant-text-tertiary))]">
                {displayFormat}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2 mt-[1px] text-[11px] font-mono text-[hsl(var(--assistant-text-tertiary))]">
            <span className="truncate">{subtitle}</span>
            <StatusIndicator status={executionStatus} timeMs={executionTimeMs} />
          </div>
        </div>

        <button
          type="button"
          onClick={handleCopy}
          disabled={view === "preview"}
          className={cn(
            "act-btn act-hover inline-flex items-center gap-1 h-[26px] px-2 rounded-md border-0 bg-transparent",
            "text-[11.5px] font-medium transition-colors",
            view === "preview"
              ? "text-[hsl(var(--assistant-text-tertiary))] opacity-50 cursor-not-allowed"
              : "text-[hsl(var(--assistant-text-secondary))] hover:text-[hsl(var(--assistant-text-primary))]",
          )}
          aria-label={t("common.copy", "Copy")}
        >
          {copied ? (
            <>
              <Check className="h-3 w-3 text-[hsl(var(--assistant-accent))]" />
              <span>Copied</span>
            </>
          ) : (
            <>
              <Copy className="h-3 w-3" />
              <span>Copy</span>
            </>
          )}
        </button>

        <button
          type="button"
          onClick={onClose}
          className={cn(
            "act-btn act-hover inline-flex items-center justify-center w-[26px] h-[26px] rounded-md border-0 bg-transparent",
            "text-[hsl(var(--assistant-text-secondary))]",
          )}
          aria-label={t("common.close", "Close")}
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* Tabs row */}
      <div
        className={cn(
          "flex items-center justify-between px-4 py-2 shrink-0",
          "border-b border-[hsl(var(--assistant-border-soft))]",
        )}
      >
        <ViewToggle view={view} onChange={setView} hasCode={hasCode} />
        {onRerun && executionStatus !== "running" && (
          <button
            type="button"
            onClick={onRerun}
            className={cn(
              "act-btn act-hover inline-flex items-center gap-1.5 h-7 px-2 rounded-md border-0 bg-transparent",
              "text-[11.5px] font-medium text-[hsl(var(--assistant-text-secondary))] hover:text-[hsl(var(--assistant-text-primary))]",
            )}
          >
            <Play className="h-3 w-3" />
            Rerun
          </button>
        )}
      </div>

      {/* Content Area */}
      <div className="flex-1 overflow-hidden">
        <AnimatePresence mode="wait">
          {view === "preview" && (
            <motion.div
              key="preview"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="act-scroll h-full overflow-y-auto p-4 space-y-5"
            >
              {ASSISTANT_UI_V2 ? (
                <>
                  {(currentRunImages.length > 0 || currentRunDocuments.length > 0) && (
                    <div className="space-y-2.5">
                      <h3 className="text-[10px] font-mono font-semibold tracking-wider uppercase text-[hsl(var(--assistant-text-tertiary))]">
                        {t("assistant.workspace.currentRun", "Current run")}
                      </h3>
                      {currentRunImages.length > 0 && (
                        <div className="grid grid-cols-2 gap-2.5">
                          {currentRunImages.map((item, idx) => (
                            <ImageCard
                              key={`run-img-${idx}`}
                              item={item}
                              onDownload={() => handleDownload(item)}
                            />
                          ))}
                        </div>
                      )}
                      <div className="space-y-2">
                        {currentRunDocuments.map((item, idx) => (
                          <ArtifactCard
                            key={`run-doc-${idx}`}
                            artifact={item}
                            onDownload={() => handleDownload(item)}
                          />
                        ))}
                      </div>
                    </div>
                  )}

                  {(sessionImages.length > 0 || sessionDocuments.length > 0) && (
                    <div className="space-y-2.5">
                      <h3 className="text-[10px] font-mono font-semibold tracking-wider uppercase text-[hsl(var(--assistant-text-tertiary))]">
                        {t("assistant.workspace.sessionArtifacts", "Session artifacts")}
                      </h3>
                      {sessionImages.length > 0 && (
                        <div className="grid grid-cols-2 gap-2.5">
                          {sessionImages.map((item) => (
                            <ImageCard
                              key={item.id}
                              item={item}
                              onDownload={() => handleDownload(item)}
                            />
                          ))}
                        </div>
                      )}
                      <div className="space-y-2">
                        {sessionDocuments.map((item) => (
                          <ArtifactCard
                            key={item.id}
                            artifact={item}
                            onDownload={() => handleDownload(item)}
                          />
                        ))}
                      </div>
                    </div>
                  )}

                  {totalCount === 0 && (
                    <div className="flex flex-col items-center justify-center h-full text-[hsl(var(--assistant-text-tertiary))]">
                      <FileText className="h-8 w-8 mb-2 opacity-40" />
                      <p className="text-[13px]">
                        {t("assistant.artifactsEmpty", "No files yet")}
                      </p>
                      <p className="text-[11px] font-mono mt-1 opacity-75">
                        {t(
                          "assistant.artifactsEmptyHint",
                          "Generated files will appear here.",
                        )}
                      </p>
                    </div>
                  )}
                </>
              ) : (
                <>
                  {imageFiles.length > 0 && (
                    <div className="space-y-2.5">
                      <h3 className="text-[10px] font-mono font-semibold tracking-wider uppercase text-[hsl(var(--assistant-text-tertiary))]">
                        Images ({imageFiles.length})
                      </h3>
                      <div className="grid grid-cols-2 gap-2.5">
                        {imageFiles.map((item, idx) => (
                          <ImageCard
                            key={"id" in item ? item.id : `img-${idx}`}
                            item={item}
                            onDownload={() => handleDownload(item)}
                          />
                        ))}
                      </div>
                    </div>
                  )}

                  {documentFiles.length > 0 && (
                    <div className="space-y-2.5">
                      <h3 className="text-[10px] font-mono font-semibold tracking-wider uppercase text-[hsl(var(--assistant-text-tertiary))]">
                        Files
                      </h3>
                      <div className="space-y-2">
                        {documentFiles.map((item, idx) => (
                          <ArtifactCard
                            key={"id" in item ? item.id : `file-${idx}`}
                            artifact={item}
                            onDownload={() => handleDownload(item)}
                          />
                        ))}
                      </div>
                    </div>
                  )}

                  {imageFiles.length === 0 && documentFiles.length === 0 && (
                    <div className="flex flex-col items-center justify-center h-full text-[hsl(var(--assistant-text-tertiary))]">
                      <FileText className="h-8 w-8 mb-2 opacity-40" />
                      <p className="text-[13px]">
                        {t("assistant.artifactsEmpty", "No files yet")}
                      </p>
                    </div>
                  )}
                </>
              )}
            </motion.div>
          )}

          {view === "code" && (
            <motion.div
              key="code"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="act-scroll h-full overflow-y-auto"
            >
              <pre
                className={cn(
                  "p-4 text-[12px] font-mono leading-relaxed whitespace-pre-wrap break-words",
                  "text-[hsl(var(--assistant-text-primary))] bg-[hsl(var(--assistant-chip-bg))]",
                )}
              >
                {currentCode || (
                  <span className="italic text-[hsl(var(--assistant-text-tertiary))]">
                    {t("assistant.noCode", "No code available")}
                  </span>
                )}
              </pre>
            </motion.div>
          )}

          {view === "output" && (
            <motion.div
              key="output"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="act-scroll h-full overflow-y-auto bg-[hsl(var(--assistant-chip-bg))]"
            >
              <pre
                className={cn(
                  "p-4 text-[12px] font-mono leading-relaxed whitespace-pre-wrap break-words",
                  "text-[hsl(var(--assistant-text-primary))]",
                )}
              >
                {executionOutput || (
                  <span className="italic text-[hsl(var(--assistant-text-tertiary))]">
                    {t("assistant.noOutput", "No output yet")}
                  </span>
                )}
              </pre>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </motion.div>
  );
}

export default ArtifactsPanel;
