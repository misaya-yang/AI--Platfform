import React, { useState } from "react";
import { Edit3, Eye, EyeOff, Trash2 } from "lucide-react";

import type { Segment } from "@/types/knowledge";
import { Badge } from "@/components/ui/badge";

export function SegmentCard({
  segment,
  index,
  onEdit,
  onDelete,
}: {
  segment: Segment;
  index: number;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const charCount = segment.char_count || segment.text?.length || 0;

  return (
    <div className="group border border-border/60 rounded-xl hover:border-primary/30 hover:shadow-md transition-all duration-200 bg-card overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2.5 bg-gradient-to-r from-muted/70 to-card border-b border-border/60">
        <div className="flex items-center gap-3">
          <span className="inline-flex items-center justify-center w-7 h-7 rounded-lg bg-gradient-to-br from-primary to-accent text-primary-foreground text-xs font-bold shadow-sm">
            {String(index + 1).padStart(2, "0")}
          </span>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="text-xs font-mono bg-white/70">
              {charCount} 字符
            </Badge>
            {segment.token_count && (
              <Badge variant="outline" className="text-xs font-mono bg-white/70">
                ~{segment.token_count} tokens
              </Badge>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            className="px-2.5 py-1 text-xs font-medium text-primary hover:text-primary/90 hover:bg-primary/10 rounded-md transition-colors"
            onClick={onEdit}
          >
            <Edit3 className="h-3 w-3 inline mr-1" />
            编辑
          </button>
          <button
            className="px-2.5 py-1 text-xs font-medium text-muted-foreground hover:text-foreground hover:bg-muted rounded-md transition-colors"
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? (
              <>
                <EyeOff className="h-3 w-3 inline mr-1" />收起
              </>
            ) : (
              <>
                <Eye className="h-3 w-3 inline mr-1" />展开
              </>
            )}
          </button>
          <button
            className="px-2.5 py-1 text-xs font-medium text-rose-500 hover:text-rose-600 hover:bg-rose-50 rounded-md transition-colors"
            onClick={onDelete}
          >
            <Trash2 className="h-3 w-3" />
          </button>
        </div>
      </div>

      <div className="px-4 py-3">
        <p
          className={`text-sm text-foreground/80 whitespace-pre-wrap leading-relaxed ${
            expanded ? "" : "line-clamp-3"
          }`}
        >
          {segment.text}
        </p>
        {!expanded && charCount > 150 && (
          <button
            onClick={() => setExpanded(true)}
            className="mt-2 text-xs text-primary hover:text-primary/90"
          >
            显示全部内容...
          </button>
        )}
      </div>
    </div>
  );
}
