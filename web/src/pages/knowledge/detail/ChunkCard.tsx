/**
 * Chunk Card Component - Alibaba Cloud Style
 * 
 * Design principles:
 * - Full-width horizontal cards (单列横向展开)
 * - Clean header with index + exact character count
 * - Content preview with expand/collapse
 * - Parent-child relationship visualization
 * - Consistent 600 char chunks like Alibaba Cloud
 */

import { useState, useMemo } from "react";
import { 
  ChevronDown, 
  ChevronUp,
  CornerDownRight
} from "lucide-react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/utils";

interface Chunk {
  segment_id: string;
  text: string;
  position: number;
  level?: number;
  parent_segment_id?: string;
  summary?: string;
  children?: Chunk[];
}

interface ChunkCardProps {
  chunk: Chunk;
  index: number;
  isChild?: boolean;
  parentIndex?: string;
  onEdit?: (segmentId: string, text: string) => void;
  onDelete?: (segmentId: string) => void;
}

// Format number with commas
function formatNumber(n: number): string {
  return n.toLocaleString();
}

// Estimate tokens (4 chars = 1 token)
function estimateTokens(chars: number): number {
  return Math.ceil(chars / 4);
}

// Child chunk - compact, indented
function ChildChunkCard({ 
  child, 
  index,
  parentIndex,
  onEdit,
  onDelete,
}: { 
  child: Chunk; 
  index: number;
  parentIndex: string;
  onEdit?: (segmentId: string, text: string) => void;
  onDelete?: (segmentId: string) => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  
  const charCount = child.text?.length || 0;
  const tokenCount = estimateTokens(charCount);
  
  // Extract first sentence for preview
  const firstSentence = useMemo(() => {
    const match = child.text?.match(/^[^。！.!?]+[。！.!?]?/);
    return match ? match[0] : child.text?.slice(0, 80);
  }, [child.text]);
  
  return (
    <div className="group/child relative">
      {/* Indentation line */}
      <div className="absolute left-3 top-0 bottom-0 w-px bg-linear-to-b from-blue-400/50 via-blue-400/30 to-transparent" />
      
      <div className="ml-8 relative">
        {/* Connector */}
        <div className="absolute -left-5 top-5 w-4 h-px bg-blue-400/50" />
        <div className="absolute -left-5 top-5 w-1.5 h-1.5 rounded-full bg-blue-400" />
        
        <div className={cn(
          "rounded-lg border transition-all duration-200",
          "bg-blue-50/30 dark:bg-blue-950/20",
          "border-blue-200/60 dark:border-blue-900/40",
          "hover:border-blue-300 dark:hover:border-blue-700"
        )}>
          {/* Header */}
          <div className="flex items-center gap-3 px-4 py-2.5 border-b border-blue-100/50 dark:border-blue-900/30">
            <span className="text-xs font-semibold text-blue-600 dark:text-blue-400">
              {parentIndex}.{String(index + 1).padStart(2, "0")}
            </span>
            
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span className="font-mono">{formatNumber(charCount)} {t("knowledge.segment.chars")}</span>
              <span className="text-muted-foreground/50">|</span>
              <span className="font-mono">~{tokenCount} tokens</span>
            </div>
            
            {charCount > 150 && (
              <button
                onClick={() => setExpanded(!expanded)}
                className="ml-auto text-xs text-blue-600 dark:text-blue-400 hover:underline"
              >
                {expanded ? t("knowledge.segment.collapse") : t("knowledge.segment.expand")}
              </button>
            )}
            {(onEdit || onDelete) && (
              <div className="ml-2 flex items-center gap-1">
                {onEdit && (
                  <button
                    onClick={() => onEdit(child.segment_id, child.text)}
                    className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
                  >
                    {t("common.edit")}
                  </button>
                )}
                {onDelete && (
                  <button
                    onClick={() => onDelete(child.segment_id)}
                    className="text-xs text-rose-500 hover:underline"
                  >
                    {t("common.delete")}
                  </button>
                )}
              </div>
            )}
          </div>
          
          {/* Content - blue highlight for first sentence */}
          <div className="px-4 py-3">
            <p className={cn(
              "text-sm leading-relaxed",
              expanded ? "" : "line-clamp-2"
            )}>
              <span className="text-blue-700 dark:text-blue-300 font-medium">
                {firstSentence}
              </span>
              {child.text?.slice(firstSentence?.length || 0)}
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}

// Parent chunk - full width, contains children
export function ChunkCard({ 
  chunk, 
  index,
  isChild = false,
  parentIndex = "",
  onEdit,
  onDelete,
}: ChunkCardProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(true);
  const [showAllChildren, setShowAllChildren] = useState(false);
  // FIX: Move useState to top level to follow React Hooks rules
  const [contentExpanded, setContentExpanded] = useState(false);
  
  const charCount = chunk.text?.length || 0;
  const tokenCount = estimateTokens(charCount);
  const children = chunk.children || [];
  const totalChildChars = children.reduce((sum, c) => sum + (c.text?.length || 0), 0);
  
  const displayIndex = isChild 
    ? `${parentIndex}.${String(index + 1).padStart(2, "0")}`
    : String(index + 1).padStart(2, "0");
  
  // Preview children (max 5)
  const previewChildren = showAllChildren ? children : children.slice(0, 5);
  const hasMoreChildren = children.length > 5;
  
  // If it's a pure child (no children of its own), render simple expandable card
  const shouldTruncate = charCount > 300;
  if (!children.length) {
    
    return (
      <div className={cn(
        "rounded-xl border transition-all duration-200",
        "bg-card border-border/60",
        "hover:border-primary/30 hover:shadow-xs"
      )}>
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border/60">
          <div className="flex items-center gap-4">
            <span className="inline-flex items-center justify-center w-10 h-10 rounded-lg bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 font-bold text-sm">
              {displayIndex}
            </span>
            
        <div className="flex items-center gap-3">
          <span className="text-sm font-medium text-foreground">
            {formatNumber(charCount)} {t("knowledge.segment.chars")}
          </span>
          <span className="text-xs text-muted-foreground">
            ~{tokenCount} tokens
          </span>
        </div>
        {(onEdit || onDelete) && (
          <div className="ml-auto flex items-center gap-1">
            {onEdit && (
              <button
                onClick={() => onEdit(chunk.segment_id, chunk.text)}
                className="px-2.5 py-1 text-xs font-medium text-primary hover:bg-primary/10 rounded-md transition-colors"
              >
                {t("common.edit")}
              </button>
            )}
            {onDelete && (
              <button
                onClick={() => onDelete(chunk.segment_id)}
                className="px-2.5 py-1 text-xs font-medium text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-950/50 rounded-md transition-colors"
              >
                {t("common.delete")}
              </button>
            )}
          </div>
        )}
      </div>
          
          {/* Expand/collapse button */}
          {shouldTruncate && (
            <button
              onClick={() => setContentExpanded(!contentExpanded)}
              className="flex items-center gap-1 px-3 py-1.5 text-xs font-medium text-slate-600 dark:text-slate-400 hover:bg-slate-100 dark:hover:bg-slate-800 rounded-lg transition-colors"
            >
              {contentExpanded ? (
                <>
                  <ChevronUp className="w-3.5 h-3.5" />
                  {t("knowledge.segment.collapse")}
                </>
              ) : (
                <>
                  <ChevronDown className="w-3.5 h-3.5" />
                  {t("knowledge.segment.expand")}
                </>
              )}
            </button>
          )}
        </div>
        
        {/* Content */}
        <div className="px-4 py-3">
          <p className={cn(
            "text-sm text-foreground/80 whitespace-pre-wrap leading-relaxed",
            contentExpanded ? "" : "line-clamp-4"
          )}>
            {chunk.text}
          </p>
          
          {/* Show hint when truncated */}
          {!contentExpanded && shouldTruncate && (
            <p className="mt-2 text-xs text-muted-foreground">
              {t("knowledge.segment.truncated", { count: charCount })}
            </p>
          )}
        </div>
      </div>
    );
  }
  
  // Parent chunk with children
  return (
    <div className={cn(
      "rounded-xl border-2 transition-all duration-200",
      "bg-card border-border",
      "hover:border-slate-300 dark:hover:border-slate-800"
    )}>
      {/* Parent Header */}
      <div 
        className="flex items-center gap-4 px-4 py-3 bg-slate-50/80 dark:bg-slate-950/50 border-b border-border cursor-pointer"
        onClick={() => setExpanded(!expanded)}
      >
        <span className="inline-flex items-center justify-center w-10 h-10 rounded-lg bg-linear-to-br from-slate-600 to-slate-800 text-white font-bold text-sm shadow-xs">
          {String(index).padStart(2, "0")}
        </span>
        
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3">
            <span className="text-sm font-semibold text-foreground">
              {formatNumber(charCount + totalChildChars)} {t("knowledge.segment.chars")}
            </span>
            <span className="text-xs text-muted-foreground">
              (~{estimateTokens(charCount + totalChildChars)} tokens)
            </span>
            {children.length > 0 && (
              <span className="text-xs px-2 py-0.5 rounded-full bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300">
                {children.length} {t("knowledge.segment.childChunks")}
              </span>
            )}
          </div>
          
          {/* Parent content preview */}
          {chunk.summary && (
            <p className="text-xs text-muted-foreground mt-1 truncate">
              {chunk.summary}
            </p>
          )}
        </div>
        
        <div className="ml-auto flex items-center gap-1">
          {(onEdit || onDelete) && (
            <>
              {onEdit && (
                <button
                  onClick={(event) => {
                    event.stopPropagation();
                    onEdit(chunk.segment_id, chunk.text);
                  }}
                  className="px-2.5 py-1 text-xs font-medium text-primary hover:bg-primary/10 rounded-md transition-colors"
                >
                  {t("common.edit")}
                </button>
              )}
              {onDelete && (
                <button
                  onClick={(event) => {
                    event.stopPropagation();
                    onDelete(chunk.segment_id);
                  }}
                  className="px-2.5 py-1 text-xs font-medium text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-950/50 rounded-md transition-colors"
                >
                  {t("common.delete")}
                </button>
              )}
            </>
          )}
          <button className="p-1.5 rounded-md hover:bg-slate-200 dark:hover:bg-slate-800 transition-colors">
          {expanded ? (
            <ChevronUp className="w-4 h-4 text-slate-500" />
          ) : (
            <ChevronDown className="w-4 h-4 text-slate-500" />
          )}
          </button>
        </div>
      </div>
      
      {/* Children container */}
      {expanded && children.length > 0 && (
        <div className="p-4 space-y-3 bg-slate-50/30 dark:bg-slate-950/30">
          {/* Section label */}
          <div className="flex items-center gap-2 text-xs text-muted-foreground mb-2">
            <CornerDownRight className="w-3.5 h-3.5" />
            <span>{t("knowledge.segment.childChunks")} ({t("knowledge.segment.retrievalUnits")})</span>
          </div>
          
          {previewChildren.map((child, idx) => (
            <ChildChunkCard
              key={child.segment_id}
              child={child}
              index={idx}
              parentIndex={String(index + 1).padStart(2, "0")}
              onEdit={onEdit}
              onDelete={onDelete}
            />
          ))}
          
          {/* Show more */}
          {hasMoreChildren && (
            <button
              onClick={() => setShowAllChildren(!showAllChildren)}
              className="w-full py-2 text-xs text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-950/30 rounded-lg transition-colors"
            >
              {showAllChildren 
                ? t("knowledge.segment.showLess", { count: children.length - 5 })
                : t("knowledge.segment.showMore", { count: children.length - 5 })
              }
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default ChunkCard;
