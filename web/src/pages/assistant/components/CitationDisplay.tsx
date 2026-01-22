/**
 * Citation Display Component
 *
 * Phase 3: Displays RAG citations with source attribution and quality indicators.
 * Shows which sources were used to generate the response with relevance scores.
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { motion, AnimatePresence } from "framer-motion";
import {
  BookOpen,
  ChevronDown,
  ChevronUp,
  ExternalLink,
  CheckCircle2,
  AlertCircle,
  Info,
} from "lucide-react";
import { cn } from "@/lib/utils";
import type { RAGCitation, RAGEvaluation, RAGQualityBreakdown } from "../types";

interface CitationDisplayProps {
  citations: RAGCitation[];
  evaluation?: RAGEvaluation;
  className?: string;
}

/** Quality score indicator with color coding */
function QualityBadge({ score }: { score: number }) {
  const { t } = useTranslation();

  const getColor = () => {
    if (score >= 80) return "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400";
    if (score >= 60) return "bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400";
    return "bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400";
  };

  const getLabel = () => {
    if (score >= 80) return t("assistant.ragQualityHigh", "High");
    if (score >= 60) return t("assistant.ragQualityMedium", "Medium");
    return t("assistant.ragQualityLow", "Low");
  };

  return (
    <span className={cn("px-2 py-0.5 rounded-full text-[10px] font-medium", getColor())}>
      {getLabel()} ({Math.round(score)})
    </span>
  );
}

/** Single citation item */
function CitationItem({ citation, index }: { citation: RAGCitation; index: number }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

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
        return t("assistant.citationUsed", "Cited");
      case "implicit":
        return t("assistant.citationImplicit", "Used");
      default:
        return t("assistant.citationUnused", "Retrieved");
    }
  };

  return (
    <motion.div
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05 }}
      className={cn(
        "border rounded-lg overflow-hidden",
        citation.status === "used"
          ? "border-emerald-200 dark:border-emerald-800/50 bg-emerald-50/30 dark:bg-emerald-900/10"
          : citation.status === "implicit"
            ? "border-blue-200 dark:border-blue-800/50 bg-blue-50/30 dark:bg-blue-900/10"
            : "border-slate-200 dark:border-slate-700/50 bg-slate-50/30 dark:bg-slate-800/10"
      )}
    >
      {/* Header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left hover:bg-white/50 dark:hover:bg-slate-700/30 transition-colors"
      >
        <span className="flex-shrink-0 w-5 h-5 flex items-center justify-center rounded bg-slate-200/80 dark:bg-slate-700/80 text-[10px] font-medium text-slate-600 dark:text-slate-300">
          {index + 1}
        </span>

        {getStatusIcon()}

        <div className="flex-1 min-w-0">
          <span className="text-xs font-medium text-slate-700 dark:text-slate-300 truncate block">
            {citation.source_title || citation.dataset_name}
          </span>
        </div>

        <span className="text-[10px] text-slate-500 dark:text-slate-400">
          {getStatusLabel()}
        </span>

        <span className="text-[10px] text-slate-400 dark:text-slate-500">
          {(citation.relevance_score * 100).toFixed(0)}%
        </span>

        {expanded ? (
          <ChevronUp className="h-3.5 w-3.5 text-slate-400" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5 text-slate-400" />
        )}
      </button>

      {/* Expanded content */}
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="border-t border-slate-200/80 dark:border-slate-700/50"
          >
            <div className="px-3 py-2 space-y-2">
              {/* Source URL */}
              {citation.source_url && (
                <a
                  href={citation.source_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-1.5 text-xs text-blue-600 dark:text-blue-400 hover:underline"
                >
                  <ExternalLink className="h-3 w-3" />
                  <span className="truncate">{citation.source_url}</span>
                </a>
              )}

              {/* Context preview */}
              {citation.context_preview && (
                <div className="text-xs text-slate-600 dark:text-slate-400 bg-white/50 dark:bg-slate-800/50 rounded p-2 leading-relaxed">
                  <span className="text-slate-400 dark:text-slate-500">Preview: </span>
                  {citation.context_preview.length > 200
                    ? `${citation.context_preview.slice(0, 200)}...`
                    : citation.context_preview}
                </div>
              )}

              {/* Cited text */}
              {citation.cited_text && citation.status === "used" && (
                <div className="text-xs text-slate-700 dark:text-slate-300 bg-emerald-50/50 dark:bg-emerald-900/20 rounded p-2 border-l-2 border-emerald-400">
                  <span className="text-emerald-600 dark:text-emerald-400 font-medium">
                    {t("assistant.citedText", "Cited")}:
                  </span>{" "}
                  {citation.cited_text}
                </div>
              )}

              {/* Metadata */}
              <div className="flex items-center gap-3 text-[10px] text-slate-500 dark:text-slate-400">
                <span>Dataset: {citation.dataset_name}</span>
                <span>Chunk: {citation.chunk_id.slice(0, 8)}</span>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

/** Quality breakdown visualization */
function QualityBreakdown({ breakdown }: { breakdown: RAGQualityBreakdown }) {
  const { t } = useTranslation();

  const items = [
    { key: "relevance", label: t("assistant.ragRelevance", "Relevance"), value: breakdown.relevance, max: 25 },
    { key: "coverage", label: t("assistant.ragCoverage", "Coverage"), value: breakdown.coverage, max: 25 },
    { key: "usage", label: t("assistant.ragUsage", "Usage"), value: breakdown.usage, max: 25 },
    { key: "citations", label: t("assistant.ragCitations", "Citations"), value: breakdown.citations, max: 25 },
  ];

  return (
    <div className="grid grid-cols-4 gap-1.5">
      {items.map((item) => (
        <div key={item.key} className="text-center">
          <div className="h-1.5 bg-slate-200 dark:bg-slate-700 rounded-full overflow-hidden">
            <div
              className={cn(
                "h-full rounded-full transition-all",
                item.value >= 20 ? "bg-emerald-500" : item.value >= 15 ? "bg-amber-500" : "bg-red-500"
              )}
              style={{ width: `${(item.value / item.max) * 100}%` }}
            />
          </div>
          <span className="text-[9px] text-slate-500 dark:text-slate-400 mt-0.5 block">
            {item.label}
          </span>
        </div>
      ))}
    </div>
  );
}

export function CitationDisplay({ citations, evaluation, className }: CitationDisplayProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

  if (!citations || !Array.isArray(citations) || citations.length === 0) return null;

  // Show all citations (retrieved sources), not just "used" ones
  // This makes sense because users want to see all sources that were retrieved and considered
  const relevantCitations = citations;
  const usedCount = citations.filter((c) => c.status === "used").length;

  return (
    <div className={cn("mt-3", className)}>
      {/* Header with summary */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-slate-100/80 dark:bg-slate-800/60 hover:bg-slate-200/80 dark:hover:bg-slate-700/60 transition-colors text-left"
      >
        <BookOpen className="h-4 w-4 text-slate-500" />

        <div className="flex-1 min-w-0">
          <span className="text-xs font-medium text-slate-700 dark:text-slate-300">
            {t("assistant.citationsTitle", "{{count}} Sources", { count: relevantCitations.length })}
          </span>
          {usedCount > 0 && (
            <span className="text-[10px] text-slate-500 dark:text-slate-400 ml-2">
              ({t("assistant.citedCount", "{{count}} cited", { count: usedCount })})
            </span>
          )}
        </div>

        {evaluation && <QualityBadge score={evaluation.quality_score} />}

        {expanded ? (
          <ChevronUp className="h-4 w-4 text-slate-400" />
        ) : (
          <ChevronDown className="h-4 w-4 text-slate-400" />
        )}
      </button>

      {/* Expanded content */}
      <AnimatePresence>
        {expanded && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            className="overflow-hidden"
          >
            <div className="mt-2 space-y-2">
              {/* Quality breakdown */}
              {evaluation && (
                <div className="px-3 py-2 bg-slate-50/50 dark:bg-slate-800/30 rounded-lg">
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-[10px] font-medium text-slate-600 dark:text-slate-400">
                      {t("assistant.ragQuality", "Response Quality")}
                    </span>
                    <span className="text-xs font-medium text-slate-700 dark:text-slate-300">
                      {Math.round(evaluation.quality_score)}/100
                    </span>
                  </div>
                  <QualityBreakdown breakdown={evaluation.quality_breakdown} />
                  <div className="flex items-center justify-between mt-2 text-[10px] text-slate-500 dark:text-slate-400">
                    <span>
                      {t("assistant.ragGrounding", "{{percent}}% grounded", {
                        percent: Math.round(evaluation.response_grounding * 100),
                      })}
                    </span>
                    <span>
                      {t("assistant.ragChunksUsed", "{{used}}/{{total}} chunks used", {
                        used: evaluation.chunks_used,
                        total: evaluation.chunks_retrieved,
                      })}
                    </span>
                  </div>
                </div>
              )}

              {/* Citation list */}
              <div className="space-y-1.5">
                {relevantCitations.map((citation, index) => (
                  <CitationItem key={citation.citation_id} citation={citation} index={index} />
                ))}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
