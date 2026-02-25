/**
 * Artifacts Panel - Claude-inspired design
 *
 * A refined, minimal panel for displaying generated documents,
 * code output, and files with elegant transitions and typography.
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
import type { ExecutionStatusType } from "./ExecutionStatus";
import { copyToClipboard } from "@/lib/clipboard";
import { useTranslation } from "react-i18next";

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
// Helpers
// ============================================================================

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function getFormatLabel(format?: string, mimeType?: string): string {
  if (format) return format.toUpperCase();
  if (mimeType?.includes("word")) return "DOCX";
  if (mimeType?.includes("pdf")) return "PDF";
  if (mimeType?.startsWith("image/")) return "IMG";
  return "FILE";
}

function getFormatColor(format?: string, mimeType?: string): string {
  const f = format?.toLowerCase() || mimeType || "";
  if (f.includes("docx") || f.includes("word")) return "bg-blue-500";
  if (f.includes("pdf")) return "bg-red-500";
  if (f.includes("md") || f.includes("markdown")) return "bg-slate-600";
  if (f.includes("xlsx") || f.includes("excel")) return "bg-green-500";
  if (f.includes("image") || f.includes("png") || f.includes("jpg")) return "bg-purple-500";
  if (f.includes("csv")) return "bg-emerald-500";
  return "bg-slate-400";
}

// ============================================================================
// Sub-components
// ============================================================================

/** Status indicator with elegant animation */
function StatusIndicator({ status, timeMs }: { status: ExecutionStatusType; timeMs?: number }) {
  if (status === "idle") return null;

  return (
    <div className="flex items-center gap-2">
      {status === "running" && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="flex items-center gap-1.5 text-xs text-amber-600 dark:text-amber-400"
        >
          <Loader2 className="h-3 w-3 animate-spin" />
          <span className="font-medium">Running</span>
        </motion.div>
      )}
      {status === "success" && (
        <motion.div
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          className="flex items-center gap-1.5 text-xs text-emerald-600 dark:text-emerald-400"
        >
          <Check className="h-3 w-3" />
          <span className="font-medium">
            Ready{timeMs ? ` · ${(timeMs / 1000).toFixed(1)}s` : ""}
          </span>
        </motion.div>
      )}
      {status === "error" && (
        <motion.div
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          className="flex items-center gap-1.5 text-xs text-red-500"
        >
          <X className="h-3 w-3" />
          <span className="font-medium">Error</span>
        </motion.div>
      )}
      {status === "timeout" && (
        <motion.div
          initial={{ opacity: 0, scale: 0.9 }}
          animate={{ opacity: 1, scale: 1 }}
          className="flex items-center gap-1.5 text-xs text-amber-500"
        >
          <X className="h-3 w-3" />
          <span className="font-medium">Timeout</span>
        </motion.div>
      )}
    </div>
  );
}

/** View toggle (Preview / Code / Output) - Claude style */
function ViewToggle({
  view,
  onChange,
  hasCode,
}: {
  view: "preview" | "code" | "output";
  onChange: (v: "preview" | "code" | "output") => void;
  hasCode: boolean;
}) {
  return (
    <div className="inline-flex items-center rounded-lg bg-slate-100 dark:bg-slate-800 p-0.5">
      <button
        onClick={() => onChange("preview")}
        className={cn(
          "flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md transition-all duration-200",
          view === "preview"
            ? "bg-white dark:bg-slate-700 text-slate-900 dark:text-white shadow-sm"
            : "text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-300"
        )}
      >
        <Eye className="h-3.5 w-3.5" />
        Preview
      </button>
      {hasCode && (
        <button
          onClick={() => onChange("code")}
          className={cn(
            "flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md transition-all duration-200",
            view === "code"
              ? "bg-white dark:bg-slate-700 text-slate-900 dark:text-white shadow-sm"
              : "text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-300"
          )}
        >
          <Code2 className="h-3.5 w-3.5" />
          Code
        </button>
      )}
      <button
        onClick={() => onChange("output")}
        className={cn(
          "flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md transition-all duration-200",
          view === "output"
            ? "bg-white dark:bg-slate-700 text-slate-900 dark:text-white shadow-sm"
            : "text-slate-500 dark:text-slate-400 hover:text-slate-700 dark:hover:text-slate-300"
        )}
      >
        <Terminal className="h-3.5 w-3.5" />
        Output
      </button>
    </div>
  );
}

/** Single artifact card in the files list */
function ArtifactCard({
  artifact,
  onDownload,
}: {
  artifact: Artifact | (OutputFile & { title?: string });
  onDownload: () => void;
}) {
  const isOutputFile = "content_base64" in artifact;
  const title = isOutputFile
    ? (artifact as OutputFile).filename
    : (artifact as Artifact).title || (artifact as Artifact).filename;
  const format = isOutputFile
    ? (artifact as OutputFile).mime_type?.split("/")[1] || "file"
    : (artifact as Artifact).format;
  const size = isOutputFile
    ? (artifact as OutputFile).size_bytes
    : (artifact as Artifact).sizeBytes;
  const mimeType = isOutputFile
    ? (artifact as OutputFile).mime_type
    : (artifact as Artifact).mimeType;

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="group flex items-center gap-3 p-3 rounded-xl bg-white dark:bg-slate-800/50 border border-slate-200/80 dark:border-slate-700/50 hover:border-slate-300 dark:hover:border-slate-600 hover:shadow-sm transition-all duration-200"
    >
      {/* Format badge */}
      <div
        className={cn(
          "flex items-center justify-center w-10 h-10 rounded-lg text-white text-[10px] font-bold tracking-wide",
          getFormatColor(format, mimeType || undefined)
        )}
      >
        {getFormatLabel(format, mimeType || undefined)}
      </div>

      {/* File info */}
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-slate-900 dark:text-slate-100 truncate">
          {title}
        </p>
        <p className="text-xs text-slate-500 dark:text-slate-400">
          {format?.toUpperCase()} {size ? `· ${formatFileSize(size)}` : ""}
        </p>
      </div>

      {/* Download button */}
      <button
        onClick={onDownload}
        className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-slate-600 dark:text-slate-300 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors opacity-0 group-hover:opacity-100"
      >
        <Download className="h-3.5 w-3.5" />
        Download
      </button>
    </motion.div>
  );
}

/** Image preview card - supports both OutputFile and Artifact types */
function ImageCard({
  item,
  onDownload,
}: {
  item: OutputFile | Artifact;
  onDownload: () => void;
}) {
  const { t } = useTranslation();
  // Determine image source - could be base64 (OutputFile) or URL (Artifact)
  const isOutputFile = "content_base64" in item;
  const imageSrc = isOutputFile
    ? `data:${item.mime_type || "image/png"};base64,${item.content_base64}`
    : (item as Artifact).url;
  const filename = isOutputFile ? item.filename : (item as Artifact).filename || (item as Artifact).title;
  const fileSize = isOutputFile ? item.size_bytes : (item as Artifact).sizeBytes;

  const handleOpenInNewTab = () => {
    if (!imageSrc) return;
    const newWindow = window.open("", "_blank");
    if (newWindow) {
      const doc = newWindow.document;
      doc.title = filename || "Image";
      doc.body.style.cssText = "margin:0;display:flex;justify-content:center;align-items:center;min-height:100vh;background:#1a1a1a;";
      const img = doc.createElement("img");
      img.src = imageSrc;
      img.alt = filename || "Image";
      img.style.cssText = "max-width:100%;max-height:100vh;object-fit:contain;";
      doc.body.appendChild(img);
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className="group rounded-xl overflow-hidden bg-white dark:bg-slate-800/50 border border-slate-200/80 dark:border-slate-700/50"
    >
      {/* Image preview */}
      <div className="relative aspect-video bg-slate-100 dark:bg-slate-800 flex items-center justify-center">
        <img
          src={imageSrc}
          alt={filename || "Generated Image"}
          className="max-w-full max-h-full object-contain"
        />
        {/* Hover actions */}
        <div className="absolute inset-0 flex items-center justify-center gap-2 opacity-0 group-hover:opacity-100 transition-opacity bg-black/30">
          <button
            onClick={handleOpenInNewTab}
            className="p-2 rounded-lg bg-black/50 hover:bg-black/70 text-white backdrop-blur-sm transition-colors"
            title={t("common.openInNewTab")}
          >
            <Eye className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Info bar */}
      <div className="flex items-center justify-between px-3 py-2 border-t border-slate-200/80 dark:border-slate-700/50">
        <div className="min-w-0">
          <p className="text-xs font-medium text-slate-900 dark:text-slate-100 truncate">
            {filename || "Image"}
          </p>
          {fileSize && (
            <p className="text-[10px] text-slate-500">
              {formatFileSize(fileSize)}
            </p>
          )}
        </div>
        <button
          onClick={onDownload}
          className="flex items-center gap-1 px-2 py-1 text-[10px] font-medium text-slate-600 dark:text-slate-300 rounded-md hover:bg-slate-100 dark:hover:bg-slate-700 transition-colors"
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
    [outputFiles]
  );

  const currentRunDocuments = React.useMemo(
    () => outputFiles.filter((f) => !f.mime_type?.startsWith("image/")),
    [outputFiles]
  );

  const sessionImages = React.useMemo(
    () => artifacts.filter((a) => a.type === "image" || a.mimeType?.startsWith("image/")),
    [artifacts]
  );

  const sessionDocuments = React.useMemo(
    () =>
      artifacts.filter(
        (a) =>
          a.type !== "image" &&
          !a.mimeType?.startsWith("image/") &&
          (a.type === "document" ||
            a.type === "file" ||
            ["docx", "pdf", "md", "xlsx", "csv"].includes(a.format))
      ),
    [artifacts]
  );

  const imageFiles = React.useMemo<(Artifact | OutputFile)[]>(() => {
    const sessionIds = new Set(sessionImages.map((image) => image.id));
    const uniqueCurrentRunImages = currentRunImages.filter(
      (image) => !image.artifact_id || !sessionIds.has(image.artifact_id)
    );
    return [...sessionImages, ...uniqueCurrentRunImages];
  }, [sessionImages, currentRunImages]);

  const documentFiles = React.useMemo<(Artifact | OutputFile)[]>(() => {
    const sessionIds = new Set(sessionDocuments.map((doc) => doc.id));
    const uniqueCurrentRunDocs = currentRunDocuments.filter(
      (doc) => !doc.artifact_id || !sessionIds.has(doc.artifact_id)
    );
    return [...sessionDocuments, ...uniqueCurrentRunDocs];
  }, [sessionDocuments, currentRunDocuments]);

  const hasCode = Boolean(currentCode);

  // Get display title
  const firstDoc = currentRunDocuments.at(0) ?? sessionDocuments.at(0);
  const displayTitle = firstDoc
    ? "content_base64" in firstDoc
      ? firstDoc.filename
      : firstDoc.title || firstDoc.filename
    : "Artifacts";
  const displayFormat = firstDoc
    ? "mime_type" in firstDoc
      ? firstDoc.mime_type?.split("/")[1]?.toUpperCase()
      : firstDoc.format?.toUpperCase()
    : "";

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
      transition={{ duration: 0.2, ease: "easeOut" }}
      className={cn(
        "flex flex-col h-full bg-slate-50 dark:bg-slate-900 border-l border-slate-200 dark:border-slate-800",
        className
      )}
    >
      {/* Header - Claude style */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900">
        <div className="flex items-center gap-3 min-w-0">
          <div className="flex items-center gap-2.5 min-w-0">
            <span className="text-sm font-semibold text-slate-900 dark:text-slate-100 truncate">
              {displayTitle}
            </span>
            {displayFormat && (
              <span className="flex-shrink-0 px-2 py-0.5 text-[10px] font-bold rounded bg-slate-100 dark:bg-slate-800 text-slate-500 dark:text-slate-400">
                {displayFormat}
              </span>
            )}
          </div>
          <StatusIndicator status={executionStatus} timeMs={executionTimeMs} />
        </div>

        <div className="flex items-center gap-1">
          <button
            onClick={handleCopy}
            disabled={view === "preview"}
            className={cn(
              "flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-lg transition-all",
              view === "preview"
                ? "text-slate-300 dark:text-slate-600 cursor-not-allowed"
                : "text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800"
            )}
          >
            {copied ? (
              <>
                <Check className="h-3.5 w-3.5 text-emerald-500" />
                Copied
              </>
            ) : (
              <>
                <Copy className="h-3.5 w-3.5" />
                Copy
              </>
            )}
          </button>

          <button
            onClick={onClose}
            className="p-1.5 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* View Toggle */}
      <div className="flex items-center justify-between px-4 py-2.5 bg-white dark:bg-slate-900 border-b border-slate-200 dark:border-slate-800">
        <ViewToggle view={view} onChange={setView} hasCode={hasCode} />

        {onRerun && executionStatus !== "running" && (
          <button
            onClick={onRerun}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-slate-600 dark:text-slate-300 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
          >
            <Play className="h-3.5 w-3.5" />
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
              className="h-full overflow-y-auto p-4 space-y-4"
            >
              {ASSISTANT_UI_V2 ? (
                <>
                  {currentRunImages.length > 0 || currentRunDocuments.length > 0 ? (
                    <div className="space-y-3">
                      <h3 className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider">
                        {t("assistant.workspace.currentRun", "Current run")}
                      </h3>
                      {currentRunImages.length > 0 && (
                        <div className="grid grid-cols-2 gap-3">
                          {currentRunImages.map((item, idx) => (
                            <ImageCard key={`run-img-${idx}`} item={item} onDownload={() => handleDownload(item)} />
                          ))}
                        </div>
                      )}
                      <div className="space-y-2">
                        {currentRunDocuments.map((item, idx) => (
                          <ArtifactCard key={`run-doc-${idx}`} artifact={item} onDownload={() => handleDownload(item)} />
                        ))}
                      </div>
                    </div>
                  ) : null}

                  {sessionImages.length > 0 || sessionDocuments.length > 0 ? (
                    <div className="space-y-3">
                      <h3 className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider">
                        {t("assistant.workspace.sessionArtifacts", "Session artifacts")}
                      </h3>
                      {sessionImages.length > 0 && (
                        <div className="grid grid-cols-2 gap-3">
                          {sessionImages.map((item) => (
                            <ImageCard key={item.id} item={item} onDownload={() => handleDownload(item)} />
                          ))}
                        </div>
                      )}
                      <div className="space-y-2">
                        {sessionDocuments.map((item) => (
                          <ArtifactCard key={item.id} artifact={item} onDownload={() => handleDownload(item)} />
                        ))}
                      </div>
                    </div>
                  ) : null}

                  {currentRunImages.length === 0 &&
                    currentRunDocuments.length === 0 &&
                    sessionImages.length === 0 &&
                    sessionDocuments.length === 0 && (
                    <div className="flex flex-col items-center justify-center h-full text-slate-400">
                      <FileText className="h-12 w-12 mb-3 opacity-50" />
                      <p className="text-sm">No files yet</p>
                      <p className="text-xs mt-1">Generated files will appear here</p>
                    </div>
                  )}
                </>
              ) : (
                <>
                  {imageFiles.length > 0 && (
                    <div className="space-y-3">
                      <h3 className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider">
                        Images ({imageFiles.length})
                      </h3>
                      <div className="grid grid-cols-2 gap-3">
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
                    <div className="space-y-3">
                      <h3 className="text-xs font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wider">
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
                    <div className="flex flex-col items-center justify-center h-full text-slate-400">
                      <FileText className="h-12 w-12 mb-3 opacity-50" />
                      <p className="text-sm">No files yet</p>
                      <p className="text-xs mt-1">Generated files will appear here</p>
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
              className="h-full overflow-y-auto"
            >
              <pre className="p-4 text-xs font-mono leading-relaxed text-slate-700 dark:text-slate-300 whitespace-pre-wrap break-words">
                {currentCode || (
                  <span className="text-slate-400 italic">No code available</span>
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
              className="h-full overflow-y-auto bg-slate-900"
            >
              <pre className="p-4 text-xs font-mono leading-relaxed text-emerald-400 whitespace-pre-wrap break-words">
                {executionOutput || (
                  <span className="text-slate-500 italic">No output yet</span>
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
