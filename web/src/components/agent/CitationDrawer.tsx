/**
 * Citation Drawer Component
 *
 * Phase 1 Frontend Style Guide: RAG citation drawer with inline badges.
 * Replaces [^1][^3] with clickable underlined number badges that open
 * a slide-out drawer showing source details.
 *
 * Features:
 * - Inline citation badges with click-to-open drawer
 * - Glassmorphism drawer design
 * - Source list with highlighted snippets
 * - Quality indicators and metadata
 * - Smooth animations (Framer Motion)
 */

import { useState, useCallback, useMemo, Fragment } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  X,
  BookOpen,
  ExternalLink,
  CheckCircle2,
  AlertCircle,
  Info,
  FileText,
  Database,
  ChevronRight,
  Copy,
  Check,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { RAGCitation, RAGEvaluation } from "@/pages/assistant/types";

// =============================================================================
// Types
// =============================================================================

import { copyToClipboard } from "@/lib/clipboard";

export interface CitationDrawerProps {
  /** RAG citations from the response */
  citations: RAGCitation[];
  /** RAG evaluation metrics */
  evaluation?: RAGEvaluation;
  /** Whether drawer is open */
  isOpen: boolean;
  /** Callback to close drawer */
  onClose: () => void;
  /** Selected citation index (0-based) */
  selectedIndex?: number;
  /** Callback when citation is selected */
  onSelectCitation?: (index: number) => void;
  /** Drawer position */
  position?: "right" | "bottom";
  /** Custom class name */
  className?: string;
}

export interface CitationBadgeProps {
  /** Citation index (1-based for display) */
  index: number;
  /** Citation data */
  citation: RAGCitation;
  /** Whether this badge is selected */
  isSelected?: boolean;
  /** Click handler */
  onClick: () => void;
  /** Custom class name */
  className?: string;
}

export interface InlineCitationsProps {
  /** Text content with [^n] markers */
  content: string;
  /** RAG citations array */
  citations: RAGCitation[];
  /** Callback when a citation badge is clicked */
  onCitationClick: (index: number) => void;
  /** Currently selected citation index */
  selectedIndex?: number;
}

// =============================================================================
// Citation Badge - Inline clickable badge
// =============================================================================

export function CitationBadge({
  index,
  citation,
  isSelected = false,
  onClick,
  className,
}: CitationBadgeProps) {
  const getStatusColor = () => {
    switch (citation.status) {
      case "used":
        return isSelected
          ? "bg-emerald-500 text-white border-emerald-500"
          : "bg-emerald-100 text-emerald-700 border-emerald-300 hover:bg-emerald-200 dark:bg-emerald-900/30 dark:text-emerald-400 dark:border-emerald-700 dark:hover:bg-emerald-900/50";
      case "implicit":
        return isSelected
          ? "bg-blue-500 text-white border-blue-500"
          : "bg-blue-100 text-blue-700 border-blue-300 hover:bg-blue-200 dark:bg-blue-900/30 dark:text-blue-400 dark:border-blue-700 dark:hover:bg-blue-900/50";
      default:
        return isSelected
          ? "bg-slate-500 text-white border-slate-500"
          : "bg-slate-100 text-slate-600 border-slate-300 hover:bg-slate-200 dark:bg-slate-800/50 dark:text-slate-400 dark:border-slate-600 dark:hover:bg-slate-700/50";
    }
  };

  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center justify-center",
        "min-w-[1.25rem] h-[1.125rem] px-1",
        "text-[10px] font-medium",
        "border rounded",
        "transition-all duration-150",
        "cursor-pointer select-none",
        "transform hover:scale-105 active:scale-95",
        "underline decoration-dotted decoration-1 underline-offset-2",
        getStatusColor(),
        className
      )}
      title={`${citation.source_title || citation.dataset_name} - ${Math.round(citation.relevance_score * 100)}% 相关`}
    >
      {index}
    </button>
  );
}

// =============================================================================
// Inline Citations Parser - Replaces [^n] with CitationBadges
// =============================================================================

export function InlineCitations({
  content,
  citations,
  onCitationClick,
  selectedIndex,
}: InlineCitationsProps) {
  // Parse content and replace [^n] markers with badges
  const parsedContent = useMemo(() => {
    if (!citations || citations.length === 0) {
      return [{ type: "text" as const, content }];
    }

    // Match [^1], [^2], etc.
    const citationRegex = /\[\^(\d+)\]/g;
    const parts: Array<{ type: "text" | "citation"; content: string; index?: number }> = [];
    let lastIndex = 0;
    let match;

    while ((match = citationRegex.exec(content)) !== null) {
      // Add text before this match
      if (match.index > lastIndex) {
        parts.push({
          type: "text",
          content: content.slice(lastIndex, match.index),
        });
      }

      // Add citation badge
      const citationIndex = parseInt(match[1], 10);
      if (citationIndex >= 1 && citationIndex <= citations.length) {
        parts.push({
          type: "citation",
          content: match[0],
          index: citationIndex,
        });
      } else {
        // Keep original text if citation index is invalid
        parts.push({
          type: "text",
          content: match[0],
        });
      }

      lastIndex = match.index + match[0].length;
    }

    // Add remaining text
    if (lastIndex < content.length) {
      parts.push({
        type: "text",
        content: content.slice(lastIndex),
      });
    }

    return parts;
  }, [content, citations]);

  return (
    <>
      {parsedContent.map((part, i) => {
        if (part.type === "citation" && part.index !== undefined) {
          const citation = citations[part.index - 1];
          if (citation) {
            return (
              <CitationBadge
                key={`citation-${i}-${part.index}`}
                index={part.index}
                citation={citation}
                isSelected={selectedIndex === part.index - 1}
                onClick={() => onCitationClick(part.index! - 1)}
                className="mx-0.5 align-baseline"
              />
            );
          }
        }
        return <Fragment key={`text-${i}`}>{part.content}</Fragment>;
      })}
    </>
  );
}

// =============================================================================
// Citation Detail Card - Shown in drawer
// =============================================================================

interface CitationDetailCardProps {
  citation: RAGCitation;
  index: number;
  isSelected: boolean;
  onSelect: () => void;
}

function CitationDetailCard({ citation, index, isSelected, onSelect }: CitationDetailCardProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await copyToClipboard(citation.cited_text || citation.context_preview);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
    }
  }, [citation]);

  const getStatusIcon = () => {
    switch (citation.status) {
      case "used":
        return <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />;
      case "implicit":
        return <Info className="h-3.5 w-3.5 text-blue-500" />;
      default:
        return <AlertCircle className="h-3.5 w-3.5 text-slate-400" />;
    }
  };

  const getStatusLabel = () => {
    switch (citation.status) {
      case "used":
        return "已引用";
      case "implicit":
        return "已使用";
      default:
        return "已检索";
    }
  };

  const getStatusColor = () => {
    switch (citation.status) {
      case "used":
        return "border-emerald-200 dark:border-emerald-800/50 bg-emerald-50/30 dark:bg-emerald-900/10";
      case "implicit":
        return "border-blue-200 dark:border-blue-800/50 bg-blue-50/30 dark:bg-blue-900/10";
      default:
        return "border-slate-200 dark:border-slate-700/50 bg-slate-50/30 dark:bg-slate-800/10";
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05 }}
      className={cn(
        "border rounded-xl overflow-hidden transition-all duration-200",
        isSelected
          ? "ring-2 ring-blue-500 ring-offset-2 dark:ring-offset-slate-900"
          : "",
        getStatusColor()
      )}
    >
      {/* Header */}
      <button
        onClick={onSelect}
        className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-white/50 dark:hover:bg-slate-700/30 transition-colors"
      >
        {/* Index badge */}
        <span className="flex-shrink-0 w-6 h-6 flex items-center justify-center rounded-lg bg-slate-200/80 dark:bg-slate-700/80 text-xs font-semibold text-slate-600 dark:text-slate-300">
          {index + 1}
        </span>

        {/* Status icon */}
        {getStatusIcon()}

        {/* Title and dataset */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-slate-700 dark:text-slate-300 truncate">
              {citation.source_title || citation.dataset_name}
            </span>
          </div>
          <div className="flex items-center gap-2 mt-0.5">
            <Database className="h-3 w-3 text-slate-400" />
            <span className="text-[10px] text-slate-500 dark:text-slate-400">
              {citation.dataset_name}
            </span>
          </div>
        </div>

        {/* Status and score */}
        <div className="flex flex-col items-end gap-1">
          <span className="text-[10px] text-slate-500 dark:text-slate-400">
            {getStatusLabel()}
          </span>
          <span
            className={cn(
              "text-[10px] font-medium px-1.5 py-0.5 rounded",
              citation.relevance_score >= 0.8
                ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400"
                : citation.relevance_score >= 0.6
                  ? "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400"
                  : "bg-slate-100 text-slate-600 dark:bg-slate-800/50 dark:text-slate-400"
            )}
          >
            {Math.round(citation.relevance_score * 100)}%
          </span>
        </div>

        <ChevronRight
          className={cn(
            "h-4 w-4 text-slate-400 transition-transform",
            isSelected && "rotate-90"
          )}
        />
      </button>

      {/* Expanded content */}
      <AnimatePresence>
        {isSelected && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="border-t border-slate-200/80 dark:border-slate-700/50"
          >
            <div className="px-4 py-3 space-y-3">
              {/* Source URL */}
              {citation.source_url && (
                <a
                  href={citation.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-2 text-xs text-blue-600 dark:text-blue-400 hover:underline"
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                  <span className="truncate">{citation.source_url}</span>
                </a>
              )}

              {/* Cited text - highlighted */}
              {citation.cited_text && citation.status === "used" && (
                <div className="relative">
                  <div className="text-xs text-slate-700 dark:text-slate-300 bg-emerald-50/50 dark:bg-emerald-900/20 rounded-lg p-3 border-l-4 border-emerald-400">
                    <div className="flex items-center gap-2 mb-2">
                      <FileText className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-400" />
                      <span className="text-[10px] font-medium text-emerald-600 dark:text-emerald-400 uppercase tracking-wide">
                        引用文本
                      </span>
                    </div>
                    <p className="leading-relaxed">{citation.cited_text}</p>
                  </div>
                  <button
                    onClick={handleCopy}
                    className="absolute top-2 right-2 p-1.5 rounded-md bg-white/80 dark:bg-slate-800/80 hover:bg-white dark:hover:bg-slate-700 transition-colors"
                    title="复制引用"
                  >
                    {copied ? (
                      <Check className="h-3.5 w-3.5 text-emerald-500" />
                    ) : (
                      <Copy className="h-3.5 w-3.5 text-slate-400" />
                    )}
                  </button>
                </div>
              )}

              {/* Context preview */}
              {citation.context_preview && (
                <div className="text-xs text-slate-600 dark:text-slate-400 bg-white/50 dark:bg-slate-800/50 rounded-lg p-3 leading-relaxed">
                  <div className="flex items-center gap-2 mb-2">
                    <BookOpen className="h-3.5 w-3.5 text-slate-500" />
                    <span className="text-[10px] font-medium text-slate-500 uppercase tracking-wide">
                      上下文预览
                    </span>
                  </div>
                  {citation.context_preview.length > 300
                    ? `${citation.context_preview.slice(0, 300)}...`
                    : citation.context_preview}
                </div>
              )}

              {/* Metadata */}
              <div className="flex items-center gap-4 text-[10px] text-slate-500 dark:text-slate-400 pt-2 border-t border-slate-200/50 dark:border-slate-700/50">
                <span>Chunk: {citation.chunk_id.slice(0, 8)}...</span>
                <span>Dataset ID: {citation.dataset_id.slice(0, 8)}...</span>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

// =============================================================================
// Quality Summary Header
// =============================================================================

interface QualitySummaryProps {
  evaluation: RAGEvaluation;
  citationCount: number;
}

function QualitySummary({ evaluation, citationCount }: QualitySummaryProps) {
  const getQualityColor = (score: number) => {
    if (score >= 80) return "text-emerald-600 dark:text-emerald-400";
    if (score >= 60) return "text-amber-600 dark:text-amber-400";
    return "text-red-600 dark:text-red-400";
  };

  const getQualityLabel = (score: number) => {
    if (score >= 80) return "优质";
    if (score >= 60) return "良好";
    return "一般";
  };

  return (
    <div className="flex items-center justify-between px-4 py-3 bg-slate-50/50 dark:bg-slate-800/30 border-b border-slate-200/80 dark:border-slate-700/50">
      <div className="flex items-center gap-3">
        <BookOpen className="h-4 w-4 text-slate-500" />
        <div>
          <span className="text-sm font-medium text-slate-700 dark:text-slate-300">
            {citationCount} 个来源
          </span>
          <span className="text-xs text-slate-500 dark:text-slate-400 ml-2">
            ({evaluation.chunks_used}/{evaluation.chunks_retrieved} 已使用)
          </span>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-xs text-slate-500 dark:text-slate-400">响应质量</span>
        <span
          className={cn(
            "text-sm font-semibold",
            getQualityColor(evaluation.quality_score)
          )}
        >
          {getQualityLabel(evaluation.quality_score)} ({Math.round(evaluation.quality_score)})
        </span>
      </div>
    </div>
  );
}

// =============================================================================
// Main Citation Drawer Component
// =============================================================================

export function CitationDrawer({
  citations,
  evaluation,
  isOpen,
  onClose,
  selectedIndex,
  onSelectCitation,
  position = "right",
  className,
}: CitationDrawerProps) {
  const [internalSelectedIndex, setInternalSelectedIndex] = useState<number | undefined>(selectedIndex);

  // Sync with external selected index
  const effectiveSelectedIndex = selectedIndex ?? internalSelectedIndex;

  const handleSelectCitation = useCallback(
    (index: number) => {
      if (onSelectCitation) {
        onSelectCitation(index);
      }
      setInternalSelectedIndex(effectiveSelectedIndex === index ? undefined : index);
    },
    [onSelectCitation, effectiveSelectedIndex]
  );

  // Animation variants based on position
  const drawerVariants = {
    hidden: position === "right"
      ? { x: "100%", opacity: 0 }
      : { y: "100%", opacity: 0 },
    visible: { x: 0, y: 0, opacity: 1 },
    exit: position === "right"
      ? { x: "100%", opacity: 0 }
      : { y: "100%", opacity: 0 },
  };

  return (
    <AnimatePresence>
      {isOpen && (
        <>
          {/* Backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
            className="fixed inset-0 bg-black/20 dark:bg-black/40 backdrop-blur-sm z-40"
          />

          {/* Drawer */}
          <motion.div
            variants={drawerVariants}
            initial="hidden"
            animate="visible"
            exit="exit"
            transition={{ type: "spring", damping: 25, stiffness: 300 }}
            className={cn(
              "fixed z-50",
              // Glassmorphism effect
              "bg-white/80 dark:bg-slate-900/80",
              "backdrop-blur-xl",
              "border-slate-200/50 dark:border-slate-700/50",
              "shadow-2xl",
              // Position-specific styles
              position === "right"
                ? "top-0 right-0 h-full w-full max-w-md border-l"
                : "bottom-0 left-0 right-0 h-[60vh] max-h-[600px] border-t rounded-t-2xl",
              className
            )}
          >
            {/* Header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-slate-200/80 dark:border-slate-700/50">
              <div className="flex items-center gap-2">
                <BookOpen className="h-5 w-5 text-slate-600 dark:text-slate-400" />
                <h3 className="text-base font-semibold text-slate-800 dark:text-slate-200">
                  引用来源
                </h3>
              </div>
              <button
                onClick={onClose}
                className="p-2 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 transition-colors"
              >
                <X className="h-5 w-5 text-slate-500" />
              </button>
            </div>

            {/* Quality summary */}
            {evaluation && (
              <QualitySummary
                evaluation={evaluation}
                citationCount={citations.length}
              />
            )}

            {/* Citation list */}
            <div
              className={cn(
                "overflow-y-auto p-4 space-y-3",
                evaluation
                  ? "h-[calc(100%-120px)]"
                  : "h-[calc(100%-60px)]"
              )}
            >
              {citations.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-12 text-slate-500 dark:text-slate-400">
                  <BookOpen className="h-12 w-12 mb-3 opacity-30" />
                  <p className="text-sm">暂无引用来源</p>
                </div>
              ) : (
                citations.map((citation, index) => (
                  <CitationDetailCard
                    key={citation.citation_id}
                    citation={citation}
                    index={index}
                    isSelected={effectiveSelectedIndex === index}
                    onSelect={() => handleSelectCitation(index)}
                  />
                ))
              )}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}

// =============================================================================
// Hook for managing citation drawer state
// =============================================================================

export interface UseCitationDrawerOptions {
  citations: RAGCitation[];
  evaluation?: RAGEvaluation;
}

export function useCitationDrawer({ citations, evaluation }: UseCitationDrawerOptions) {
  const [isOpen, setIsOpen] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState<number | undefined>();

  const openDrawer = useCallback((index?: number) => {
    setSelectedIndex(index);
    setIsOpen(true);
  }, []);

  const closeDrawer = useCallback(() => {
    setIsOpen(false);
  }, []);

  const handleCitationClick = useCallback((index: number) => {
    setSelectedIndex(index);
    setIsOpen(true);
  }, []);

  return {
    isOpen,
    selectedIndex,
    openDrawer,
    closeDrawer,
    handleCitationClick,
    // Render helper
    drawerProps: {
      citations,
      evaluation,
      isOpen,
      onClose: closeDrawer,
      selectedIndex,
      onSelectCitation: setSelectedIndex,
    },
  };
}

export default CitationDrawer;
