/**
 * Hierarchical Segment Card - Parent-Child Chunk Display
 * 
 * Design inspired by Dify and Alibaba Cloud Knowledge Base
 * - Parent chunks provide context (larger, collapsible)
 * - Child chunks are highlighted in blue for precise matching
 * - Exact character count display for each chunk
 */

import { useState, useMemo } from "react";
import { 
  ChevronDown, 
  ChevronRight, 
  Layers, 
  Type, 
  Hash,
  Sparkles,
  Eye,
  EyeOff
} from "lucide-react";
import { useTranslation } from "react-i18next";

import type { Segment } from "@/types/knowledge";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

// Extended segment with hierarchical info
interface HierarchicalSegment extends Segment {
  level?: 1 | 2 | 3; // 1=document, 2=section(parent), 3=paragraph(child)
  parent_segment_id?: string;
  children?: HierarchicalSegment[];
  summary?: string;
  keywords?: string[];
}

// FIX: Unified callback signatures to match ChunkCard.tsx
interface HierarchicalSegmentCardProps {
  segment: HierarchicalSegment;
  index: number;
  onEdit?: (segmentId: string, text: string) => void;
  onDelete?: (segmentId: string) => void;
  defaultExpanded?: boolean;
}

// Calculate exact character count (excluding whitespace for cleaner display)
function getCharCount(text?: string): number {
  if (!text) return 0;
  return text.length;
}

// Calculate token count estimate (~4 chars per token for mixed content)
function estimateTokens(charCount: number): number {
  return Math.ceil(charCount / 4);
}

// FIX: Unicode-safe string slicing to avoid cutting surrogate pairs (emoji, etc.)
function safeSlice(text: string, length: number): [string, string] {
  if (!text) return ['', ''];
  const chars = Array.from(text);
  return [
    chars.slice(0, length).join(''),
    chars.slice(length).join('')
  ];
}

// FIX: Child content component with Unicode-safe text highlighting
interface ChildContentProps {
  text?: string;
  expanded: boolean;
}

function ChildContent({ text, expanded }: ChildContentProps) {
  const [highlightPart, restPart] = useMemo(() => {
    if (!text) return ['', ''];
    return safeSlice(text, 50);
  }, [text]);

  return (
    <p className={cn(
      "text-sm leading-relaxed",
      "text-foreground/90",
      expanded ? "" : "line-clamp-2"
    )}>
      <span className="text-blue-600 dark:text-blue-400 font-medium">
        {highlightPart}
      </span>
      {restPart}
    </p>
  );
}

// Child chunk component - compact, blue highlight
function ChildChunk({ 
  child, 
  index: childIndex,
  parentIndex,
  onEdit,
  onDelete,
}: { 
  child: HierarchicalSegment; 
  index: number;
  parentIndex: number;
  onEdit?: (segmentId: string, text: string) => void;
  onDelete?: (segmentId: string) => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  
  const charCount = getCharCount(child.text);
  const tokenCount = estimateTokens(charCount);
  
  return (
    <div className="group/child relative">
      {/* Child chunk card with blue accent */}
      <div className="relative ml-4 pl-4 border-l-2 border-blue-300 dark:border-blue-700">
        {/* Connector dot */}
        <div className="absolute left-[-5px] top-3 w-2 h-2 rounded-full bg-blue-400 dark:bg-blue-600 ring-2 ring-background" />
        
        <div className={cn(
          "relative rounded-lg border transition-all duration-200",
          "bg-linear-to-r from-blue-50/80 to-blue-50/40 dark:from-blue-950/30 dark:to-blue-950/20",
          "border-blue-200 dark:border-blue-900/60",
          "hover:border-blue-300 dark:hover:border-blue-700",
          "hover:shadow-xs"
        )}>
          {/* Child header */}
          <div className="flex items-center justify-between px-3 py-2 border-b border-blue-100 dark:border-blue-900/50">
            <div className="flex items-center gap-2">
              {/* Child index badge */}
              <span className="inline-flex items-center justify-center px-2 py-0.5 rounded text-[10px] font-bold bg-blue-100 dark:bg-blue-900/50 text-blue-700 dark:text-blue-300">
                {parentIndex}.{childIndex + 1}
              </span>
              
              {/* Character count - prominent */}
              <Badge variant="outline" className="text-[10px] font-mono bg-white/70 dark:bg-gray-900/50 border-blue-200 dark:border-blue-900/60">
                <Type className="w-3 h-3 mr-1 text-blue-500" />
                {charCount.toLocaleString()} {t("knowledge.segment.chars")}
              </Badge>
              
              {/* Token estimate */}
              <span className="text-[10px] text-muted-foreground">
                ~{tokenCount} tokens
              </span>
            </div>
            
            {/* Expand/collapse */}
            <div className="flex items-center gap-1">
              {(onEdit || onDelete) && (
                <>
                  {onEdit && (
                    <button
                      onClick={() => onEdit(child.segment_id, child.text || "")}
                      className="px-2 py-0.5 text-[10px] text-blue-600 dark:text-blue-400 hover:bg-blue-100 dark:hover:bg-blue-900/50 rounded transition-colors"
                    >
                      {t("common.edit")}
                    </button>
                  )}
                  {onDelete && (
                    <button
                      onClick={() => onDelete(child.segment_id)}
                      className="px-2 py-0.5 text-[10px] text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-950/50 rounded transition-colors"
                    >
                      {t("common.delete")}
                    </button>
                  )}
                </>
              )}
              <button
                onClick={() => setExpanded(!expanded)}
                className="p-1 rounded hover:bg-blue-100 dark:hover:bg-blue-900/50 transition-colors"
                aria-label={expanded ? t("knowledge.segment.collapse") : t("knowledge.segment.expand")}
                aria-expanded={expanded}
              >
                {expanded ? (
                  <EyeOff className="w-3.5 h-3.5 text-blue-600 dark:text-blue-400" />
                ) : (
                  <Eye className="w-3.5 h-3.5 text-blue-600 dark:text-blue-400" />
                )}
              </button>
            </div>
          </div>
          
          {/* Child content - highlighted text */}
          <div className="px-3 py-2">
            {/* FIX: Use Unicode-safe slice to prevent cutting surrogate pairs */}
            <ChildContent text={child.text} expanded={expanded} />
            
            {!expanded && charCount > 100 && (
              <button
                onClick={() => setExpanded(true)}
                className="mt-1 text-xs text-blue-600 dark:text-blue-400 hover:underline"
              >
                {t("knowledge.segment.showMore")}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// Parent chunk component - larger, contains children
export function HierarchicalSegmentCard({
  segment,
  index,
  onEdit,
  onDelete,
  defaultExpanded = true,
}: HierarchicalSegmentCardProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [showAllChildren, setShowAllChildren] = useState(false);
  
  const children = segment.children || [];
  
  // Calculate statistics
  const parentCharCount = getCharCount(segment.text);
  const totalChildChars = children.reduce((sum, child) => sum + getCharCount(child.text), 0);
  const totalChars = parentCharCount + totalChildChars;
  
  // Get preview children (first 3)
  const previewChildren = showAllChildren ? children : children.slice(0, 3);
  const hasMoreChildren = children.length > 3;
  
  // Extract heading from metadata or first line
  const heading = useMemo(() => {
    if (segment.summary) return segment.summary;
    const firstLine = segment.text?.split('\n')[0]?.slice(0, 80);
    return firstLine || `${t("knowledge.segment.section")} ${index + 1}`;
  }, [segment.text, segment.summary, index, t]);

  return (
    <div className="group/parent">
      {/* Parent chunk - main container */}
      <div className={cn(
        "relative rounded-xl border-2 transition-all duration-300",
        "bg-card",
        "border-border",
        "hover:border-slate-300 dark:hover:border-slate-800",
        "hover:shadow-lg hover:shadow-slate-200/50 dark:hover:shadow-black/20",
        "overflow-hidden"
      )}>
        {/* Parent header - prominent */}
        <div className={cn(
          "flex items-center justify-between px-4 py-3",
          "bg-linear-to-r from-slate-50 via-slate-50/80 to-card",
          "dark:from-slate-950 dark:via-slate-950/80 dark:to-card",
          "border-b border-border"
        )}>
          <div className="flex items-center gap-3">
            {/* Parent index - large badge */}
            <span className={cn(
              "inline-flex items-center justify-center",
              "w-9 h-9 rounded-xl",
              "bg-linear-to-br from-slate-600 to-slate-800",
              "dark:from-slate-500 dark:to-slate-700",
              "text-white text-sm font-bold shadow-md"
            )}>
              {String(index + 1).padStart(2, "0")}
            </span>
            
            {/* Title and stats */}
            <div className="flex flex-col gap-0.5">
              <span className="text-sm font-semibold text-foreground line-clamp-1 max-w-[400px]">
                {heading}
              </span>
              
              {/* Stats row */}
              <div className="flex items-center gap-2">
                {/* Total characters - most prominent */}
                <Badge 
                  variant="secondary" 
                  className="text-xs font-mono bg-slate-100 dark:bg-slate-900 text-slate-700 dark:text-slate-300"
                >
                  <Type className="w-3 h-3 mr-1" />
                  {totalChars.toLocaleString()} {t("knowledge.segment.totalChars")}
                </Badge>
                
                {/* Parent-only stats */}
                {parentCharCount > 0 && (
                  <Badge 
                    variant="outline" 
                    className="text-[10px] font-mono text-muted-foreground"
                  >
                    <Type className="w-3 h-3 mr-1" />
                    {parentCharCount.toLocaleString()} {t("knowledge.segment.parentChars")}
                  </Badge>
                )}
                
                {/* Token estimate */}
                <span className="text-[10px] text-muted-foreground">
                  ~{estimateTokens(totalChars)} tokens
                </span>
                
                {/* Child count */}
                {children.length > 0 && (
                  <Badge 
                    variant="outline" 
                    className="text-[10px] bg-blue-50 dark:bg-blue-950/30 border-blue-200 dark:border-blue-900/60"
                  >
                    <Layers className="w-3 h-3 mr-1 text-blue-500" />
                    {children.length} {t("knowledge.segment.childChunks")}
                  </Badge>
                )}
              </div>
            </div>
          </div>
          
          {/* Actions */}
          <div className="flex items-center gap-1">
            {/* FIX: Add edit/delete buttons for parent chunk */}
            {(onEdit || onDelete) && (
              <>
                {onEdit && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onEdit(segment.segment_id, segment.text || "");
                    }}
                    className="px-2.5 py-1 text-xs font-medium text-primary hover:bg-primary/10 rounded-md transition-colors"
                  >
                    {t("common.edit")}
                  </button>
                )}
                {onDelete && (
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete(segment.segment_id);
                    }}
                    className="px-2.5 py-1 text-xs font-medium text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-950/50 rounded-md transition-colors"
                  >
                    {t("common.delete")}
                  </button>
                )}
              </>
            )}
            {/* Expand/collapse parent */}
            <button
              onClick={() => setExpanded(!expanded)}
              className={cn(
                "flex items-center gap-1 px-3 py-1.5 text-xs font-medium rounded-lg",
                "text-slate-600 dark:text-slate-400",
                "hover:bg-slate-100 dark:hover:bg-slate-800",
                "transition-colors"
              )}
              aria-expanded={expanded}
              aria-label={expanded ? t("knowledge.segment.collapse") : t("knowledge.segment.expand")}
            >
              {expanded ? (
                <>
                  <ChevronDown className="w-4 h-4" />
                  {t("knowledge.segment.collapse")}
                </>
              ) : (
                <>
                  <ChevronRight className="w-4 h-4" />
                  {t("knowledge.segment.expand")}
                </>
              )}
            </button>
          </div>
        </div>
        
        {/* Parent content preview */}
        {expanded && parentCharCount > 0 && (
          <div className="px-4 py-3 bg-slate-50/50 dark:bg-slate-950/30 border-b border-slate-100 dark:border-slate-900">
            <div className="flex items-center gap-2 mb-2">
              <Sparkles className="w-3.5 h-3.5 text-amber-500" />
              <span className="text-xs font-medium text-muted-foreground">
                {t("knowledge.segment.context")}
              </span>
            </div>
            <p className="text-sm text-foreground/70 line-clamp-2 leading-relaxed">
              {segment.text}
            </p>
          </div>
        )}
        
        {/* Children container */}
        {expanded && children.length > 0 && (
          <div className="p-4 space-y-3 bg-linear-to-b from-slate-50/30 to-transparent dark:from-slate-950/20">
            {/* Section label */}
            <div className="flex items-center gap-2 mb-3">
              <div className="h-px flex-1 bg-linear-to-r from-blue-200 to-transparent dark:from-blue-900" />
              <span className="text-xs font-medium text-blue-600 dark:text-blue-400 flex items-center gap-1">
                <Hash className="w-3 h-3" />
                {t("knowledge.segment.retrievalUnits")}
              </span>
              <div className="h-px flex-1 bg-linear-to-l from-blue-200 to-transparent dark:from-blue-900" />
            </div>
            
            {/* Child chunks */}
            <div className="space-y-2">
              {previewChildren.map((child, childIdx) => (
                <ChildChunk
                  key={child.segment_id}
                  child={child}
                  index={childIdx}
                  parentIndex={index + 1}
                  onEdit={onEdit}
                  onDelete={onDelete}
                />
              ))}
            </div>
            
            {/* Show more/less */}
            {hasMoreChildren && (
              <button
                onClick={() => setShowAllChildren(!showAllChildren)}
                className={cn(
                  "w-full py-2 text-xs font-medium rounded-lg",
                  "text-blue-600 dark:text-blue-400",
                  "hover:bg-blue-50 dark:hover:bg-blue-950/30",
                  "transition-colors"
                )}
              >
                {showAllChildren 
                  ? t("knowledge.segment.showLess", { count: children.length - 3 })
                  : t("knowledge.segment.showMore", { count: children.length - 3 })
                }
              </button>
            )}
          </div>
        )}
        
        {/* Collapsed state - summary */}
        {!expanded && children.length > 0 && (
          <div className="px-4 py-3 bg-slate-50/50 dark:bg-slate-950/30">
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Layers className="w-4 h-4 text-blue-500" />
              <span>
                {children.length} {t("knowledge.segment.childChunks")} · {" "}
                {totalChildChars.toLocaleString()} {t("knowledge.segment.chars")}
              </span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// Simple segment card for non-hierarchical mode (original style but improved)
export function SimpleSegmentCard({
  segment,
  index,
  onEdit,
  onDelete,
}: HierarchicalSegmentCardProps) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  
  const charCount = getCharCount(segment.text);
  const tokenCount = estimateTokens(charCount);
  
  return (
    <div className={cn(
      "group relative rounded-xl border transition-all duration-200",
      "bg-card border-border/60",
      "hover:border-primary/30 hover:shadow-md"
    )}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border/60 bg-linear-to-r from-muted/50 to-card">
        <div className="flex items-center gap-3">
          {/* Index */}
          <span className={cn(
            "inline-flex items-center justify-center w-8 h-8 rounded-lg",
            "bg-linear-to-br from-slate-600 to-slate-800 text-white",
            "text-sm font-bold shadow-xs"
          )}>
            {String(index + 1).padStart(2, "0")}
          </span>
          
          {/* Character count - prominent */}
          <div className="flex items-center gap-2">
            <Badge 
              variant="secondary" 
              className="text-xs font-mono bg-white dark:bg-gray-800"
            >
              <Type className="w-3 h-3 mr-1" />
              {charCount.toLocaleString()} {t("knowledge.segment.chars")}
            </Badge>
            <span className="text-xs text-muted-foreground">
              ~{tokenCount} tokens
            </span>
          </div>
        </div>
        
        {/* Actions */}
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          {onEdit && (
            <button
              onClick={() => onEdit(segment.segment_id, segment.text || "")}
              className="px-2.5 py-1 text-xs font-medium text-primary hover:bg-primary/10 rounded-md transition-colors"
            >
              {t("common.edit")}
            </button>
          )}
          <button
            onClick={() => setExpanded(!expanded)}
            className="px-2.5 py-1 text-xs font-medium text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors"
            aria-expanded={expanded}
            aria-label={expanded ? t("knowledge.segment.collapse") : t("knowledge.segment.expand")}
          >
            {expanded ? t("knowledge.segment.collapse") : t("knowledge.segment.expand")}
          </button>
          {onDelete && (
            <button
              onClick={() => onDelete(segment.segment_id)}
              className="px-2.5 py-1 text-xs font-medium text-rose-500 hover:bg-rose-50 dark:hover:bg-rose-950/50 rounded-md transition-colors"
            >
              {t("common.delete")}
            </button>
          )}
        </div>
      </div>
      
      {/* Content */}
      <div className="px-4 py-3">
        <p className={cn(
          "text-sm text-foreground/80 whitespace-pre-wrap leading-relaxed",
          expanded ? "" : "line-clamp-3"
        )}>
          {segment.text}
        </p>
        {!expanded && charCount > 200 && (
          <button
            onClick={() => setExpanded(true)}
            className="mt-2 text-xs text-primary hover:underline"
          >
            {t("knowledge.segment.showAllContent")}
          </button>
        )}
      </div>
    </div>
  );
}

export default HierarchicalSegmentCard;
