import { useState } from "react";
import { History, RefreshCcw, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { Document } from "@/types/knowledge";
import { StatusBadge } from "@/pages/knowledge/detail/StatusBadge";
import { Checkbox } from "@/components/ui/checkbox";
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
import { DocumentVersionHistory } from "./DocumentVersionHistory";

export function DocumentRow({
  doc,
  datasetId,
  selected,
  checked,
  onSelect,
  onCheck,
  onReindex,
  onDelete,
  onVersionRestored,
  showCheckbox = false,
}: {
  doc: Document;
  datasetId: string;
  selected: boolean;
  checked?: boolean;
  onSelect: () => void;
  onCheck?: (checked: boolean) => void;
  onReindex: () => Promise<void>;
  onDelete: () => Promise<void>;
  onVersionRestored?: () => void;
  showCheckbox?: boolean;
}) {
  const { t, i18n } = useTranslation();
  const [loading, setLoading] = useState(false);
  const [reindexOpen, setReindexOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [versionHistoryOpen, setVersionHistoryOpen] = useState(false);

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
          flex flex-wrap items-center gap-y-3 px-4 py-3 border-b border-border/60 last:border-b-0 hover:bg-muted/40 transition-colors
          sm:flex-nowrap sm:gap-y-0 sm:px-5
          ${selected ? "bg-primary/5" : ""} ${checked ? "bg-primary/10" : ""}
        `}
      >
        {showCheckbox && (
          <div className="mr-3 flex items-center">
            <Checkbox
              checked={checked}
              onCheckedChange={(val) => onCheck?.(val === true)}
              onClick={(e) => e.stopPropagation()}
            />
          </div>
        )}
        <div className="flex min-w-0 basis-full items-center gap-3 sm:basis-auto sm:flex-1">
          {getFileIcon()}
          <button
            type="button"
            className="min-w-0 truncate text-left text-sm font-medium text-primary hover:text-primary/90"
            onClick={onSelect}
          >
            {doc.title}
          </button>
        </div>

        <div className="hidden w-24 text-sm text-muted-foreground text-center sm:block">
          {formatFileSize(doc.size_bytes)}
        </div>

        <div className="order-2 flex w-auto justify-start sm:order-none sm:w-28 sm:justify-center">
          <StatusBadge 
            status={doc.status} 
            error={doc.error} 
            progress={doc.progress} 
            metadata={doc.metadata}
          />
        </div>

        <div className="hidden w-28 text-sm text-muted-foreground text-center sm:block">{t("knowledge.documentRow.defaultCategory")}</div>

        <div className="hidden w-40 text-sm text-muted-foreground text-center sm:block">
          {doc.created_at
            ? new Date(doc.created_at).toLocaleString(i18n.language === "zh-CN" ? "zh-CN" : "en-US", {
                year: "numeric",
                month: "2-digit",
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
              })
            : "-"}
        </div>

        <div className="order-3 ml-auto flex w-auto flex-wrap justify-end gap-1 text-sm sm:order-none sm:ml-0 sm:w-48 sm:gap-2">
          <button
            className="min-h-10 px-1 text-primary hover:text-primary/90 disabled:cursor-not-allowed disabled:opacity-50 sm:min-h-0 sm:px-0"
            onClick={(e) => {
              e.stopPropagation();
              onSelect();
            }}
            disabled={loading}
          >
            {t("knowledge.documentRow.segments")}
          </button>
          <button
            className="inline-flex h-10 w-10 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50 sm:h-7 sm:w-7"
            onClick={(e) => {
              e.stopPropagation();
              setVersionHistoryOpen(true);
            }}
            disabled={loading}
            title={t("knowledge.documentRow.versionHistoryTitle")}
            aria-label={t("knowledge.documentRow.versionHistoryTitle")}
          >
            <History className="h-3.5 w-3.5" />
          </button>
          <button
            className="inline-flex h-10 w-10 items-center justify-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50 sm:h-7 sm:w-7"
            onClick={(e) => {
              e.stopPropagation();
              setReindexOpen(true);
            }}
            disabled={loading}
            title={t("knowledge.documentRow.reindexTitle")}
            aria-label={t("knowledge.documentRow.reindexTitle")}
          >
            <RefreshCcw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          </button>
          <button
            className="inline-flex h-10 w-10 items-center justify-center rounded-md text-muted-foreground hover:bg-destructive/10 hover:text-destructive disabled:cursor-not-allowed disabled:opacity-50 sm:h-7 sm:w-7"
            onClick={(e) => {
              e.stopPropagation();
              setDeleteOpen(true);
            }}
            disabled={loading}
            title={t("common.delete")}
            aria-label={t("common.delete")}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>

      <AlertDialog open={reindexOpen} onOpenChange={setReindexOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("knowledge.documentRow.confirmReindex")}</AlertDialogTitle>
            <AlertDialogDescription className="space-y-2">
              <span className="block">
                {t("knowledge.documentRow.reindexDesc", { title: doc.title })}
              </span>
              <span className="block text-xs text-muted-foreground">
                {t("knowledge.documentRow.reindexHint")}
              </span>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                setReindexOpen(false);
                handleAction(onReindex);
              }}
            >
              {t("knowledge.documentRow.confirmRebuild")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("knowledge.documentRow.confirmDeleteDoc")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("knowledge.documentRow.deleteDocDesc", { title: doc.title })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              className="bg-rose-600 hover:bg-rose-700"
              onClick={() => {
                setDeleteOpen(false);
                handleAction(onDelete);
              }}
            >
              {t("knowledge.documentRow.confirmDelete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <DocumentVersionHistory
        datasetId={datasetId}
        documentId={doc.document_id}
        documentTitle={doc.title || t("knowledge.documentRow.untitledDocument")}
        open={versionHistoryOpen}
        onOpenChange={setVersionHistoryOpen}
        onRestored={onVersionRestored}
      />
    </>
  );
}
