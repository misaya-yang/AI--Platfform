import { useState } from "react";

import type { Document } from "@/types/knowledge";
import { StatusBadge } from "@/pages/knowledge/detail/StatusBadge";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";

export function DocumentRow({
  doc,
  selected,
  onSelect,
  onReindex,
  onDelete,
}: {
  doc: Document;
  selected: boolean;
  onSelect: () => void;
  onReindex: () => Promise<void>;
  onDelete: () => Promise<void>;
}) {
  const [loading, setLoading] = useState(false);
  const [reindexOpen, setReindexOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);

  const handleAction = async (action: () => Promise<void>) => {
    if (loading) return;
    setLoading(true);
    try {
      await action();
    } finally {
      setLoading(false);
    }
  };

  const formatFileSize = (bytes?: number) => {
    if (!bytes) return "-";
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)}MB`;
  };

  const getFileIcon = () => {
    const ext = doc.title?.split(".").pop()?.toLowerCase() || "";
    if (["pdf"].includes(ext)) {
      return (
        <div className="w-6 h-6 rounded bg-rose-500 flex items-center justify-center text-white text-xs font-bold">
          P
        </div>
      );
    }
    if (["doc", "docx"].includes(ext)) {
      return (
        <div className="w-6 h-6 rounded bg-amber-500 flex items-center justify-center text-white text-xs font-bold">
          W
        </div>
      );
    }
    if (["xls", "xlsx"].includes(ext)) {
      return (
        <div className="w-6 h-6 rounded bg-emerald-500 flex items-center justify-center text-white text-xs font-bold">
          X
        </div>
      );
    }
    return (
      <div className="w-6 h-6 rounded bg-slate-500 flex items-center justify-center text-white text-xs font-bold">
        T
      </div>
    );
  };

  return (
    <>
      <div
        className={`
          flex items-center px-5 py-3 border-b border-border/60 last:border-b-0 hover:bg-muted/40 transition-colors
          ${selected ? "bg-primary/5" : ""}
        `}
      >
        <div className="flex items-center gap-3 flex-1 min-w-0">
          {getFileIcon()}
          <span
            className="truncate text-primary hover:text-primary/90 cursor-pointer font-medium text-sm"
            onClick={onSelect}
          >
            {doc.title}
          </span>
        </div>

        <div className="w-24 text-sm text-muted-foreground text-center">
          {formatFileSize(doc.size_bytes)}
        </div>

        <div className="w-28 flex justify-center">
          <StatusBadge status={doc.status} error={doc.error} progress={doc.progress} />
        </div>

        <div className="w-28 text-sm text-muted-foreground text-center">默认分类</div>

        <div className="w-40 text-sm text-muted-foreground text-center">
          {doc.created_at
            ? new Date(doc.created_at).toLocaleString("zh-CN", {
                year: "numeric",
                month: "2-digit",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
              })
            : "-"}
        </div>

        <div className="w-48 flex justify-center gap-3 text-sm">
          <button
            className="text-primary hover:text-primary/90 disabled:opacity-50 disabled:cursor-not-allowed"
            onClick={(e) => {
              e.stopPropagation();
              onSelect();
            }}
            disabled={loading}
          >
            查看切片
          </button>
          <button
            className="text-primary hover:text-primary/90 disabled:opacity-50 disabled:cursor-not-allowed"
            onClick={(e) => {
              e.stopPropagation();
              setReindexOpen(true);
            }}
            disabled={loading}
            title="重新索引"
          >
            {loading ? "处理中..." : "重建索引"}
          </button>
          <button
            className="text-rose-500 hover:text-rose-600 disabled:opacity-50 disabled:cursor-not-allowed"
            onClick={(e) => {
              e.stopPropagation();
              setDeleteOpen(true);
            }}
            disabled={loading}
          >
            删除
          </button>
        </div>
      </div>

      <AlertDialog open={reindexOpen} onOpenChange={setReindexOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认重新构建索引？</AlertDialogTitle>
            <AlertDialogDescription className="space-y-2">
              <span className="block">
                这将重新解析文档 "{doc.title}" 并生成新的切片和向量索引。
              </span>
              <span className="block text-xs text-muted-foreground">
                将使用知识库当前的分段配置。如需修改配置，请先在"配置"页面调整。
              </span>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setReindexOpen(false);
                handleAction(onReindex);
              }}
            >
              确认重建
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认删除文档？</AlertDialogTitle>
            <AlertDialogDescription>
              文档 "{doc.title}" 将被永久删除，且无法恢复。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              className="bg-rose-600 hover:bg-rose-700"
              onClick={() => {
                setDeleteOpen(false);
                handleAction(onDelete);
              }}
            >
              确认删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
