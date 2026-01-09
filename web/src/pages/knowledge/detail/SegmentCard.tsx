import { useState } from "react";
import { Edit3, Eye, EyeOff, Trash2, ImageIcon, FileText, Download, ExternalLink } from "lucide-react";

import type { Segment } from "@/types/knowledge";
import { Badge } from "@/components/ui/badge";

function formatFileSize(bytes?: number): string {
  if (!bytes) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

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
  const [imageLoaded, setImageLoaded] = useState(false);
  const [imageError, setImageError] = useState(false);

  const isImage = segment.content_type === "image";
  const charCount = segment.char_count || segment.text?.length || 0;

  // Image segment rendering
  if (isImage) {
    return (
      <div className="group border border-border/60 rounded-xl hover:border-primary/30 hover:shadow-md transition-all duration-200 bg-card overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-2.5 bg-gradient-to-r from-violet-50/80 to-card dark:from-violet-950/30 dark:to-card border-b border-border/60">
          <div className="flex items-center gap-3">
            <span className="inline-flex items-center justify-center w-7 h-7 rounded-lg bg-gradient-to-br from-violet-500 to-purple-600 text-white text-xs font-bold shadow-sm">
              <ImageIcon className="w-3.5 h-3.5" />
            </span>
            <div className="flex items-center gap-2">
              <Badge variant="outline" className="text-xs font-mono bg-violet-50 dark:bg-violet-950/50 text-violet-700 dark:text-violet-300 border-violet-200 dark:border-violet-800">
                图片
              </Badge>
              {segment.image_filename && (
                <span className="text-xs text-muted-foreground truncate max-w-[200px]" title={segment.image_filename}>
                  {segment.image_filename}
                </span>
              )}
              {segment.image_file_size && (
                <Badge variant="outline" className="text-xs font-mono bg-white/70 dark:bg-gray-800/70">
                  {formatFileSize(segment.image_file_size)}
                </Badge>
              )}
            </div>
          </div>
          <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
            {segment.image_url && (
              <a
                href={segment.image_url}
                target="_blank"
                rel="noopener noreferrer"
                className="px-2.5 py-1 text-xs font-medium text-primary hover:text-primary/90 hover:bg-primary/10 rounded-md transition-colors inline-flex items-center gap-1"
              >
                <ExternalLink className="h-3 w-3" />
                查看原图
              </a>
            )}
            <button
              className="px-2.5 py-1 text-xs font-medium text-rose-500 hover:text-rose-600 hover:bg-rose-50 dark:hover:bg-rose-950/50 rounded-md transition-colors"
              onClick={onDelete}
            >
              <Trash2 className="h-3 w-3" />
            </button>
          </div>
        </div>

        {/* Image Content */}
        <div className="p-4">
          {segment.image_url ? (
            <div className="relative">
              {!imageLoaded && !imageError && (
                <div className="flex items-center justify-center h-48 bg-muted/30 rounded-lg animate-pulse">
                  <ImageIcon className="w-8 h-8 text-muted-foreground/50" />
                </div>
              )}
              {imageError ? (
                <div className="flex flex-col items-center justify-center h-48 bg-rose-50/50 dark:bg-rose-950/20 rounded-lg border border-rose-200/50 dark:border-rose-800/50">
                  <ImageIcon className="w-8 h-8 text-rose-400 mb-2" />
                  <span className="text-sm text-rose-600 dark:text-rose-400">图片加载失败</span>
                  {segment.image_url && (
                    <a
                      href={segment.image_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="mt-2 text-xs text-primary hover:underline"
                    >
                      尝试直接打开
                    </a>
                  )}
                </div>
              ) : (
                <img
                  src={segment.image_url}
                  alt={segment.image_filename || "Segment image"}
                  className={`max-w-full h-auto max-h-96 rounded-lg shadow-sm border border-border/40 object-contain mx-auto transition-opacity duration-300 ${
                    imageLoaded ? "opacity-100" : "opacity-0 absolute"
                  }`}
                  onLoad={() => setImageLoaded(true)}
                  onError={() => setImageError(true)}
                />
              )}
            </div>
          ) : (
            <div className="flex flex-col items-center justify-center h-32 bg-muted/30 rounded-lg">
              <ImageIcon className="w-8 h-8 text-muted-foreground/50 mb-2" />
              <span className="text-sm text-muted-foreground">图片 URL 不可用</span>
            </div>
          )}

          {/* Image metadata */}
          {(segment.image_media_type || segment.text) && (
            <div className="mt-3 pt-3 border-t border-border/40">
              {segment.image_media_type && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground mb-1">
                  <span className="font-medium">类型:</span>
                  <code className="px-1.5 py-0.5 bg-muted rounded text-xs">{segment.image_media_type}</code>
                </div>
              )}
              {segment.text && (
                <div className="mt-2">
                  <span className="text-xs font-medium text-muted-foreground block mb-1">上下文:</span>
                  <p className={`text-sm text-foreground/70 whitespace-pre-wrap leading-relaxed bg-muted/30 rounded-md p-2 ${
                    expanded ? "" : "line-clamp-2"
                  }`}>
                    {segment.text}
                  </p>
                  {!expanded && segment.text.length > 100 && (
                    <button
                      onClick={() => setExpanded(true)}
                      className="mt-1 text-xs text-primary hover:text-primary/90"
                    >
                      显示全部...
                    </button>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    );
  }

  // Text segment rendering (original)
  return (
    <div className="group border border-border/60 rounded-xl hover:border-primary/30 hover:shadow-md transition-all duration-200 bg-card overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2.5 bg-gradient-to-r from-muted/70 to-card border-b border-border/60">
        <div className="flex items-center gap-3">
          <span className="inline-flex items-center justify-center w-7 h-7 rounded-lg bg-gradient-to-br from-primary to-accent text-primary-foreground text-xs font-bold shadow-sm">
            {String(index + 1).padStart(2, "0")}
          </span>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="text-xs font-mono bg-white/70 dark:bg-gray-800/70">
              {charCount} 字符
            </Badge>
            {segment.token_count && (
              <Badge variant="outline" className="text-xs font-mono bg-white/70 dark:bg-gray-800/70">
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
            className="px-2.5 py-1 text-xs font-medium text-rose-500 hover:text-rose-600 hover:bg-rose-50 dark:hover:bg-rose-950/50 rounded-md transition-colors"
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
