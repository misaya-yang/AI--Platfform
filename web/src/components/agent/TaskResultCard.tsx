/**
 * Task Result Card Component
 *
 * Phase 1 Frontend Style Guide: Structured output cards for different content types.
 * Displays code blocks, charts/images, and structured summaries in actionable card format.
 *
 * Features:
 * - Code blocks with syntax highlighting, copy, and run buttons
 * - Image/chart previews with modal and download
 * - Structured summary cards in grid layout
 * - Glassmorphism card design
 * - Smooth animations (Framer Motion)
 */

import { useState, useCallback, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Copy,
  Check,
  Play,
  Download,
  Maximize2,
  X,
  Code2,
  Image as ImageIcon,
  FileText,
  BarChart3,
  Table,
  Loader2,
  Terminal,
  ExternalLink,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { copyToClipboard } from "@/lib/clipboard";
import { useTranslation } from "react-i18next";

// =============================================================================
// Types
// =============================================================================

export type TaskResultType = "code" | "image" | "chart" | "summary" | "table" | "file";

export interface CodeBlockData {
  type: "code";
  language: string;
  code: string;
  filename?: string;
  isRunnable?: boolean;
  onRun?: (code: string) => Promise<{ output: string; success: boolean }>;
}

export interface ImageData {
  type: "image" | "chart";
  src: string;
  alt?: string;
  title?: string;
  width?: number;
  height?: number;
  downloadUrl?: string;
}

export interface SummaryData {
  type: "summary";
  title: string;
  items: Array<{
    label: string;
    value: string;
    icon?: React.ReactNode;
    highlight?: boolean;
  }>;
}

export interface TableData {
  type: "table";
  title?: string;
  headers: string[];
  rows: string[][];
  downloadUrl?: string;
}

export interface FileData {
  type: "file";
  filename: string;
  size?: number;
  mimeType?: string;
  downloadUrl: string;
  previewUrl?: string;
}

export type TaskResultData = CodeBlockData | ImageData | SummaryData | TableData | FileData;

export interface TaskResultCardProps {
  data: TaskResultData;
  className?: string;
}

// =============================================================================
// Code Block Card
// =============================================================================

interface CodeBlockCardProps {
  data: CodeBlockData;
  className?: string;
}

function CodeBlockCard({ data, className }: CodeBlockCardProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const [isRunning, setIsRunning] = useState(false);
  const [output, setOutput] = useState<string | null>(null);
  const [runSuccess, setRunSuccess] = useState<boolean | null>(null);

  const handleCopy = useCallback(async () => {
    try {
      await copyToClipboard(data.code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
    }
  }, [data.code]);

  const handleRun = useCallback(async () => {
    if (!data.onRun || isRunning) return;

    setIsRunning(true);
    setOutput(null);
    setRunSuccess(null);

    try {
      const result = await data.onRun(data.code);
      setOutput(result.output);
      setRunSuccess(result.success);
    } catch (err) {
      setOutput(String(err));
      setRunSuccess(false);
    } finally {
      setIsRunning(false);
    }
  }, [data, isRunning]);

  // Language icon and color
  const getLanguageInfo = (lang: string) => {
    const langMap: Record<string, { color: string; label: string }> = {
      python: { color: "bg-yellow-500/10 text-yellow-600 dark:text-yellow-400", label: "Python" },
      javascript: { color: "bg-yellow-500/10 text-yellow-600 dark:text-yellow-400", label: "JavaScript" },
      typescript: { color: "bg-blue-500/10 text-blue-600 dark:text-blue-400", label: "TypeScript" },
      java: { color: "bg-red-500/10 text-red-600 dark:text-red-400", label: "Java" },
      go: { color: "bg-cyan-500/10 text-cyan-600 dark:text-cyan-400", label: "Go" },
      rust: { color: "bg-orange-500/10 text-orange-600 dark:text-orange-400", label: "Rust" },
      sql: { color: "bg-blue-500/10 text-blue-600 dark:text-blue-400", label: "SQL" },
      bash: { color: "bg-slate-500/10 text-slate-600 dark:text-slate-400", label: "Bash" },
      shell: { color: "bg-slate-500/10 text-slate-600 dark:text-slate-400", label: "Shell" },
      json: { color: "bg-green-500/10 text-green-600 dark:text-green-400", label: "JSON" },
      yaml: { color: "bg-pink-500/10 text-pink-600 dark:text-pink-400", label: "YAML" },
      markdown: { color: "bg-slate-500/10 text-slate-600 dark:text-slate-400", label: "Markdown" },
    };
    return langMap[lang.toLowerCase()] || { color: "bg-slate-500/10 text-slate-500", label: lang };
  };

  const langInfo = getLanguageInfo(data.language);

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "rounded-xl overflow-hidden",
        "border border-slate-200/80 dark:border-slate-700/50",
        "bg-white/60 dark:bg-slate-900/60",
        "backdrop-blur-sm",
        "shadow-sm",
        className
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 bg-slate-50/80 dark:bg-slate-800/50 border-b border-slate-200/80 dark:border-slate-700/50">
        <div className="flex items-center gap-2">
          <Code2 className="h-4 w-4 text-slate-500" />
          {data.filename && (
            <span className="text-xs font-medium text-slate-700 dark:text-slate-300">
              {data.filename}
            </span>
          )}
          <span className={cn("text-[10px] font-medium px-2 py-0.5 rounded-full", langInfo.color)}>
            {langInfo.label}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {data.isRunnable && data.onRun && (
            <button
              onClick={handleRun}
              disabled={isRunning}
              className={cn(
                "flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-medium transition-all",
                isRunning
                  ? "bg-slate-200 dark:bg-slate-700 text-slate-400 cursor-not-allowed"
                  : "bg-emerald-100 dark:bg-emerald-900/30 text-emerald-700 dark:text-emerald-400 hover:bg-emerald-200 dark:hover:bg-emerald-900/50"
              )}
            >
              {isRunning ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5" />
              )}
              {isRunning ? t("common.running") : t("common.run")}
            </button>
          )}
          <button
            onClick={handleCopy}
            className="p-1.5 rounded-lg hover:bg-slate-200/80 dark:hover:bg-slate-700/50 transition-colors"
            title={t("common.copy")}
          >
            {copied ? (
              <Check className="h-4 w-4 text-emerald-500" />
            ) : (
              <Copy className="h-4 w-4 text-slate-500" />
            )}
          </button>
        </div>
      </div>

      {/* Code content */}
      <div className="relative">
        <pre className="p-4 overflow-x-auto text-sm leading-relaxed">
          <code className="text-slate-800 dark:text-slate-200 font-mono">
            {data.code}
          </code>
        </pre>
      </div>

      {/* Output section */}
      <AnimatePresence>
        {output !== null && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="border-t border-slate-200/80 dark:border-slate-700/50"
          >
            <div className="px-4 py-2.5 bg-slate-900 dark:bg-black">
              <div className="flex items-center gap-2 mb-2">
                <Terminal className="h-3.5 w-3.5 text-slate-400" />
                <span className="text-[10px] font-medium text-slate-400 uppercase tracking-wide">
                  {t("common.output")}
                </span>
                {runSuccess !== null && (
                  <span
                    className={cn(
                      "text-[10px] px-1.5 py-0.5 rounded",
                      runSuccess
                        ? "bg-emerald-500/20 text-emerald-400"
                        : "bg-red-500/20 text-red-400"
                    )}
                  >
                    {runSuccess ? t("common.success") : t("common.failed")}
                  </span>
                )}
              </div>
              <pre className="text-xs text-slate-300 font-mono whitespace-pre-wrap">
                {output}
              </pre>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

// =============================================================================
// Image/Chart Card
// =============================================================================

interface ImageCardProps {
  data: ImageData;
  className?: string;
}

function ImageCard({ data, className }: ImageCardProps) {
  const { t } = useTranslation();
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [isLoaded, setIsLoaded] = useState(false);

  const handleDownload = useCallback(() => {
    if (!data.downloadUrl) return;

    const link = document.createElement("a");
    link.href = data.downloadUrl;
    link.download = data.title || "image";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }, [data]);

  const isChart = data.type === "chart";
  const Icon = isChart ? BarChart3 : ImageIcon;

  return (
    <>
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        className={cn(
          "rounded-xl overflow-hidden",
          "border border-slate-200/80 dark:border-slate-700/50",
          "bg-white/60 dark:bg-slate-900/60",
          "backdrop-blur-sm",
          "shadow-sm",
          className
        )}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-2.5 bg-slate-50/80 dark:bg-slate-800/50 border-b border-slate-200/80 dark:border-slate-700/50">
          <div className="flex items-center gap-2">
            <Icon className="h-4 w-4 text-slate-500" />
            <span className="text-xs font-medium text-slate-700 dark:text-slate-300">
              {data.title || (isChart ? t("common.chart") : t("common.image"))}
            </span>
          </div>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setIsModalOpen(true)}
              className="p-1.5 rounded-lg hover:bg-slate-200/80 dark:hover:bg-slate-700/50 transition-colors"
              title={t("common.fullscreenPreview")}
            >
              <Maximize2 className="h-4 w-4 text-slate-500" />
            </button>
            {data.downloadUrl && (
              <button
                onClick={handleDownload}
                className="p-1.5 rounded-lg hover:bg-slate-200/80 dark:hover:bg-slate-700/50 transition-colors"
                title={t("common.download")}
              >
                <Download className="h-4 w-4 text-slate-500" />
              </button>
            )}
          </div>
        </div>

        {/* Image preview */}
        <div className="relative bg-slate-100/50 dark:bg-slate-800/30">
          {!isLoaded && (
            <div className="absolute inset-0 flex items-center justify-center">
              <Loader2 className="h-8 w-8 text-slate-400 animate-spin" />
            </div>
          )}
          <img
            src={data.src}
            alt={data.alt || data.title || "Result image"}
            className={cn(
              "w-full h-auto max-h-[400px] object-contain cursor-pointer transition-opacity",
              isLoaded ? "opacity-100" : "opacity-0"
            )}
            onClick={() => setIsModalOpen(true)}
            onLoad={() => setIsLoaded(true)}
          />
        </div>
      </motion.div>

      {/* Fullscreen modal */}
      <AnimatePresence>
        {isModalOpen && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm"
            onClick={() => setIsModalOpen(false)}
          >
            <motion.div
              initial={{ scale: 0.9, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0.9, opacity: 0 }}
              className="relative max-w-[90vw] max-h-[90vh]"
              onClick={(e) => e.stopPropagation()}
            >
              <img
                src={data.src}
                alt={data.alt || data.title || "Result image"}
                className="max-w-full max-h-[90vh] object-contain rounded-lg"
              />
              <button
                onClick={() => setIsModalOpen(false)}
                className="absolute top-4 right-4 p-2 rounded-full bg-black/50 hover:bg-black/70 text-white transition-colors"
              >
                <X className="h-5 w-5" />
              </button>
              {data.downloadUrl && (
                <button
                  onClick={handleDownload}
                  className="absolute bottom-4 right-4 flex items-center gap-2 px-4 py-2 rounded-lg bg-white/90 hover:bg-white text-slate-700 font-medium text-sm transition-colors"
                >
                  <Download className="h-4 w-4" />
                  {t("common.download")}
                </button>
              )}
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}

// =============================================================================
// Summary Card
// =============================================================================

interface SummaryCardProps {
  data: SummaryData;
  className?: string;
}

function SummaryCard({ data, className }: SummaryCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "rounded-xl overflow-hidden",
        "border border-slate-200/80 dark:border-slate-700/50",
        "bg-white/60 dark:bg-slate-900/60",
        "backdrop-blur-sm",
        "shadow-sm",
        className
      )}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-4 py-3 bg-slate-50/80 dark:bg-slate-800/50 border-b border-slate-200/80 dark:border-slate-700/50">
        <FileText className="h-4 w-4 text-slate-500" />
        <span className="text-sm font-medium text-slate-700 dark:text-slate-300">
          {data.title}
        </span>
      </div>

      {/* Items grid */}
      <div className="p-4 grid grid-cols-2 gap-3">
        {data.items.map((item, index) => (
          <motion.div
            key={index}
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ delay: index * 0.05 }}
            className={cn(
              "p-3 rounded-lg",
              item.highlight
                ? "bg-blue-50/80 dark:bg-blue-900/20 border border-blue-200/50 dark:border-blue-800/50"
                : "bg-slate-50/80 dark:bg-slate-800/30"
            )}
          >
            <div className="flex items-center gap-2 mb-1.5">
              {item.icon && (
                <span className="text-slate-500 dark:text-slate-400">
                  {item.icon}
                </span>
              )}
              <span className="text-[10px] font-medium text-slate-500 dark:text-slate-400 uppercase tracking-wide">
                {item.label}
              </span>
            </div>
            <div
              className={cn(
                "text-sm font-medium",
                item.highlight
                  ? "text-blue-700 dark:text-blue-300"
                  : "text-slate-700 dark:text-slate-300"
              )}
            >
              {item.value}
            </div>
          </motion.div>
        ))}
      </div>
    </motion.div>
  );
}

// =============================================================================
// Table Card
// =============================================================================

interface TableCardProps {
  data: TableData;
  className?: string;
}

function TableCard({ data, className }: TableCardProps) {
  const { t } = useTranslation();
  const tableRef = useRef<HTMLDivElement>(null);

  const handleDownload = useCallback(() => {
    if (!data.downloadUrl) return;

    const link = document.createElement("a");
    link.href = data.downloadUrl;
    link.download = data.title || t("common.tableFile");
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }, [data, t]);

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "rounded-xl overflow-hidden",
        "border border-slate-200/80 dark:border-slate-700/50",
        "bg-white/60 dark:bg-slate-900/60",
        "backdrop-blur-sm",
        "shadow-sm",
        className
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-2.5 bg-slate-50/80 dark:bg-slate-800/50 border-b border-slate-200/80 dark:border-slate-700/50">
        <div className="flex items-center gap-2">
          <Table className="h-4 w-4 text-slate-500" />
          <span className="text-xs font-medium text-slate-700 dark:text-slate-300">
            {data.title || t("common.table")}
          </span>
          <span className="text-[10px] text-slate-500 dark:text-slate-400">
            ({t("common.rows", { count: data.rows.length })})
          </span>
        </div>
        {data.downloadUrl && (
          <button
            onClick={handleDownload}
            className="flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-medium bg-slate-100 dark:bg-slate-800 text-slate-600 dark:text-slate-400 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors"
          >
            <Download className="h-3.5 w-3.5" />
            {t("common.export")}
          </button>
        )}
      </div>

      {/* Table content */}
      <div ref={tableRef} className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-slate-50/50 dark:bg-slate-800/30">
              {data.headers.map((header, index) => (
                <th
                  key={index}
                  className="px-4 py-2.5 text-left text-[10px] font-semibold text-slate-500 dark:text-slate-400 uppercase tracking-wide border-b border-slate-200/50 dark:border-slate-700/50"
                >
                  {header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row, rowIndex) => (
              <tr
                key={rowIndex}
                className="hover:bg-slate-50/50 dark:hover:bg-slate-800/30 transition-colors"
              >
                {row.map((cell, cellIndex) => (
                  <td
                    key={cellIndex}
                    className="px-4 py-2.5 text-slate-700 dark:text-slate-300 border-b border-slate-200/50 dark:border-slate-700/50"
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </motion.div>
  );
}

// =============================================================================
// File Card
// =============================================================================

interface FileCardProps {
  data: FileData;
  className?: string;
}

function FileCard({ data, className }: FileCardProps) {
  const { t } = useTranslation();
  const handleDownload = useCallback(() => {
    const link = document.createElement("a");
    link.href = data.downloadUrl;
    link.download = data.filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }, [data]);

  const formatSize = (bytes?: number) => {
    if (!bytes) return "";
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  };

  const fileIcon =
    !data.mimeType ? (
      <FileText className="h-6 w-6 text-slate-500" />
    ) : data.mimeType.startsWith("image/") ? (
      <ImageIcon className="h-6 w-6 text-slate-500" />
    ) : data.mimeType.includes("spreadsheet") || data.mimeType.includes("csv") ? (
      <Table className="h-6 w-6 text-slate-500" />
    ) : data.mimeType.includes("presentation") || data.mimeType.includes("ppt") ? (
      <BarChart3 className="h-6 w-6 text-slate-500" />
    ) : (
      <FileText className="h-6 w-6 text-slate-500" />
    );

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      className={cn(
        "flex items-center gap-4 p-4 rounded-xl",
        "border border-slate-200/80 dark:border-slate-700/50",
        "bg-white/60 dark:bg-slate-900/60",
        "backdrop-blur-sm",
        "shadow-sm",
        className
      )}
    >
      {/* File icon */}
      <div className="flex-shrink-0 w-12 h-12 flex items-center justify-center rounded-xl bg-slate-100 dark:bg-slate-800">
        {fileIcon}
      </div>

      {/* File info */}
      <div className="flex-1 min-w-0">
        <h4 className="text-sm font-medium text-slate-700 dark:text-slate-300 truncate">
          {data.filename}
        </h4>
        <div className="flex items-center gap-2 mt-1 text-xs text-slate-500 dark:text-slate-400">
          {data.size && <span>{formatSize(data.size)}</span>}
          {data.mimeType && (
            <>
              <span>·</span>
              <span>{data.mimeType.split("/").pop()?.toUpperCase()}</span>
            </>
          )}
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2">
        {data.previewUrl && (
          <a
            href={data.previewUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="p-2 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
            title={t("common.preview")}
          >
            <ExternalLink className="h-4 w-4 text-slate-500" />
          </a>
        )}
        <button
          onClick={handleDownload}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400 hover:bg-blue-200 dark:hover:bg-blue-900/50 transition-colors text-xs font-medium"
        >
          <Download className="h-3.5 w-3.5" />
          {t("common.download")}
        </button>
      </div>
    </motion.div>
  );
}

// =============================================================================
// Main TaskResultCard Component
// =============================================================================

export function TaskResultCard({ data, className }: TaskResultCardProps) {
  switch (data.type) {
    case "code":
      return <CodeBlockCard data={data} className={className} />;
    case "image":
    case "chart":
      return <ImageCard data={data} className={className} />;
    case "summary":
      return <SummaryCard data={data} className={className} />;
    case "table":
      return <TableCard data={data} className={className} />;
    case "file":
      return <FileCard data={data} className={className} />;
    default:
      return null;
  }
}

// =============================================================================
// Task Result List - Helper for rendering multiple results
// =============================================================================

export interface TaskResultListProps {
  results: TaskResultData[];
  className?: string;
}

export function TaskResultList({ results, className }: TaskResultListProps) {
  if (!results || results.length === 0) return null;

  return (
    <div className={cn("space-y-4", className)}>
      {results.map((result, index) => (
        <TaskResultCard key={`result-${index}`} data={result} />
      ))}
    </div>
  );
}

export default TaskResultCard;
