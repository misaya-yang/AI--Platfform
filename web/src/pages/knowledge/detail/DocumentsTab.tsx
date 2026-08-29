/**
 * Documents tab of the dataset detail page.
 *
 * Owns document list filtering/search, batch selection, the segment panel,
 * and their dialogs (segment edit, batch reindex, batch delete). The text /
 * URL creation dialogs live in the shell because the Sources tab opens them
 * too; their openers arrive as props.
 *
 * Extracted verbatim from DatasetDetail.tsx (C2 split); no behavior change.
 * The component stays mounted while other tabs are visible (the shell hides
 * it with `hidden`) so all state survives tab switches exactly as before.
 */

import { useMemo, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";
import { useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Upload,
  RefreshCcw,
  Trash2,
  Search,
  FileText,
  Plus,
  Loader2,
  Zap,
  Hash,
  Globe,
  X,
  ChevronDown,
  CheckSquare,
  Square,
  ListChecks,
  LayoutList,
  Table2,
  ImageIcon,
} from "lucide-react";

import { useDocuments, useSegments } from "@/hooks/useKnowledge";
import {
  deleteDocument,
  deleteSegment,
  reindexDocument,
  updateSegment,
  batchReindexDocuments,
  batchDeleteDocuments,
} from "@/api/knowledge";
import type { Document } from "@/types/knowledge";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { toast } from "@/hooks/use-toast";
import { DocumentRow } from "@/pages/knowledge/detail/DocumentRow";
import { SegmentList } from "@/pages/knowledge/detail/SegmentList";

const FILE_TYPE_CATEGORIES: Record<string, string[]> = {
  document: ["pdf", "doc", "docx", "txt", "md", "html", "rtf"],
  data: ["xls", "xlsx", "csv", "json", "xml"],
  image: ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"],
};

interface DocumentsTabProps {
  datasetId?: string;
  docs: Document[];
  docsQuery: ReturnType<typeof useDocuments>;
  fileInputRef: RefObject<HTMLInputElement | null>;
  uploading: boolean;
  openFilePicker: () => void;
  onFilesSelected: (files?: FileList | null) => void;
  onOpenTextDialog: () => void;
  onOpenUrlDialog: () => void;
}

export function DocumentsTab({
  datasetId,
  docs,
  docsQuery,
  fileInputRef,
  uploading,
  openFilePicker,
  onFilesSelected,
  onOpenTextDialog,
  onOpenUrlDialog,
}: DocumentsTabProps) {
  const { t } = useTranslation();
  const qc = useQueryClient();

  const [selectedDocId, setSelectedDocId] = useState<string | undefined>(undefined);
  const selectedDoc = useMemo(
    () => docs.find((d) => d.document_id === selectedDocId),
    [docs, selectedDocId]
  );

  const [segmentSearch, setSegmentSearch] = useState("");
  const segmentsQuery = useSegments(datasetId, selectedDocId, segmentSearch);
  const segments = segmentsQuery.data || [];

  // Segment edit dialog
  const [editOpen, setEditOpen] = useState(false);
  const [editSegmentId, setEditSegmentId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [editSaving, setEditSaving] = useState(false);

  // Batch operations
  const [batchMode, setBatchMode] = useState(false);
  const [selectedDocIds, setSelectedDocIds] = useState<Set<string>>(new Set());
  const [batchReindexOpen, setBatchReindexOpen] = useState(false);
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
  const [batchLoading, setBatchLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState<"all" | "completed" | "uploaded" | "processing" | "failed">("all");
  const [contentTypeFilter, setContentTypeFilter] = useState<"all" | "document" | "data" | "image">("all");

  // Search state
  const [searchField, setSearchField] = useState<"name" | "id">("name");
  const [searchTerm, setSearchTerm] = useState("");
  const [formatFilter, setFormatFilter] = useState("all");

  // Filter documents by status, content type, format, and search term
  const filteredDocs = useMemo(() => {
    let result = docs;

    // Filter by search term
    if (searchTerm.trim()) {
      const term = searchTerm.toLowerCase().trim();
      result = result.filter((d) => {
        if (searchField === "name") {
          return d.title?.toLowerCase().includes(term);
        } else {
          return d.document_id?.toLowerCase().includes(term);
        }
      });
    }

    // Filter by status
    if (statusFilter !== "all") {
      if (statusFilter === "processing") {
        result = result.filter((d) =>
          [
            "uploaded",
            "queued",
            "detecting",
            "processing",
            "parsing",
            "segmenting",
            "embedding",
            "embedding_images",
          ].includes(d.status)
        );
      } else {
        result = result.filter((d) => d.status === statusFilter);
      }
    }

    // Filter by format
    if (formatFilter !== "all") {
      result = result.filter((d) => {
        const ext = d.title?.split(".").pop()?.toLowerCase() || "";
        return ext === formatFilter;
      });
    }

    // Filter by content type
    if (contentTypeFilter !== "all") {
      result = result.filter((d) => {
        const ext = d.title?.split(".").pop()?.toLowerCase() || "";
        return FILE_TYPE_CATEGORIES[contentTypeFilter]?.includes(ext);
      });
    }

    return result;
  }, [docs, searchTerm, searchField, statusFilter, formatFilter, contentTypeFilter]);

  // Count documents by content type for badges
  const contentTypeCounts = useMemo(() => {
    const counts = { all: docs.length, document: 0, data: 0, image: 0 };
    docs.forEach((d) => {
      const ext = d.title?.split(".").pop()?.toLowerCase() || "";
      if (FILE_TYPE_CATEGORIES.document.includes(ext)) counts.document++;
      else if (FILE_TYPE_CATEGORIES.data.includes(ext)) counts.data++;
      else if (FILE_TYPE_CATEGORIES.image.includes(ext)) counts.image++;
    });
    return counts;
  }, [docs]);

  // Don't auto-select first document - only show segments when user clicks "查看切片"

  async function handleReindex(doc: Document) {
    if (!datasetId) return;

    try {
      await reindexDocument(datasetId, doc.document_id);
      toast.success(
        t("knowledge.detail.reindexSuccess"),
        t("knowledge.detail.reindexSuccessDesc", { title: doc.title })
      );
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      // 触发一次刷新
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      }, 1000);
    } catch (e) {
      console.error("Reindex failed:", e);
      toast.error(t("knowledge.detail.reindexFailed"), e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDeleteDoc(doc: Document) {
    if (!datasetId) return;
    await deleteDocument(datasetId, doc.document_id);
    await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
    if (selectedDocId === doc.document_id) setSelectedDocId(undefined);
  }

  // Batch operations
  function toggleBatchMode() {
    if (batchMode) {
      // Exit batch mode, clear selection
      setBatchMode(false);
      setSelectedDocIds(new Set());
    } else {
      setBatchMode(true);
    }
  }

  function toggleDocSelection(docId: string) {
    setSelectedDocIds((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(docId)) {
        newSet.delete(docId);
      } else {
        newSet.add(docId);
      }
      return newSet;
    });
  }

  function toggleSelectAll() {
    if (selectedDocIds.size === filteredDocs.length) {
      // Deselect all
      setSelectedDocIds(new Set());
    } else {
      // Select all filtered docs
      setSelectedDocIds(new Set(filteredDocs.map((d) => d.document_id)));
    }
  }

  function selectByStatus(status: string) {
    const docsWithStatus = docs.filter((d) => {
      if (status === "processing") {
        return [
          "uploaded",
          "queued",
          "detecting",
          "processing",
          "parsing",
          "segmenting",
          "embedding",
          "embedding_images",
        ].includes(d.status);
      }
      return d.status === status;
    });
    setSelectedDocIds(new Set(docsWithStatus.map((d) => d.document_id)));
  }

  async function handleBatchReindex() {
    if (!datasetId || selectedDocIds.size === 0) return;
    setBatchLoading(true);
    try {
      const result = await batchReindexDocuments(datasetId, Array.from(selectedDocIds));
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      setBatchReindexOpen(false);
      setSelectedDocIds(new Set());
      // Show result
      if (result.failed_count > 0) {
        toast.warning(t("knowledge.detail.batchReindexDone"), `${result.success_count} / ${result.failed_count}`);
      } else {
        toast.success(t("knowledge.detail.batchReindexDone"), t("knowledge.detail.batchReindexSuccess", { count: result.success_count }));
      }
    } catch (e) {
      console.error("Batch reindex failed:", e);
      toast.error(t("knowledge.detail.batchReindexFailed"), e instanceof Error ? e.message : String(e));
    } finally {
      setBatchLoading(false);
    }
  }

  async function handleBatchDelete() {
    if (!datasetId || selectedDocIds.size === 0) return;
    setBatchLoading(true);
    try {
      const result = await batchDeleteDocuments(datasetId, Array.from(selectedDocIds));
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      setBatchDeleteOpen(false);
      setSelectedDocIds(new Set());
      // Clear selected doc if it was deleted
      if (selectedDocId && selectedDocIds.has(selectedDocId)) {
        setSelectedDocId(undefined);
      }
      // Show result
      if (result.failed_count > 0) {
        toast.warning(t("knowledge.detail.batchDeleteDone"), `${result.success_count} / ${result.failed_count}`);
      } else {
        toast.success(t("knowledge.detail.batchDeleteDone"), t("knowledge.detail.batchDeleteSuccess", { count: result.success_count }));
      }
    } catch (e) {
      console.error("Batch delete failed:", e);
      toast.error(t("knowledge.detail.batchDeleteFailed"), e instanceof Error ? e.message : String(e));
    } finally {
      setBatchLoading(false);
    }
  }

  function openEdit(segmentId: string, text: string) {
    setEditSegmentId(segmentId);
    setEditText(text);
    setEditOpen(true);
  }

  async function saveEdit() {
    if (!datasetId || !editSegmentId) return;
    setEditSaving(true);
    try {
      await updateSegment(datasetId, editSegmentId, editText);
      await qc.invalidateQueries({
        queryKey: ["kb-segments", datasetId, selectedDocId, segmentSearch],
      });
    } finally {
      setEditSaving(false);
      setEditOpen(false);
    }
  }

  async function handleDeleteSegment(segmentId: string) {
    if (!datasetId) return;
    await deleteSegment(datasetId, segmentId);
    await qc.invalidateQueries({
      queryKey: ["kb-segments", datasetId, selectedDocId, segmentSearch],
    });
  }

  return (
    <div className="space-y-4">
      {/* 内容类型子Tab - 圆角药丸风格 */}
      <div className="ui-tabs-rail w-full rounded-full bg-muted/50 p-1 sm:w-auto">
        {[
          { key: "all" as const, label: t("knowledge.detail.contentTypeAll"), icon: LayoutList, count: contentTypeCounts.all },
          { key: "document" as const, label: t("knowledge.detail.contentTypeDocument"), icon: FileText, count: contentTypeCounts.document },
          { key: "data" as const, label: t("knowledge.detail.contentTypeData"), icon: Table2, count: contentTypeCounts.data },
          { key: "image" as const, label: t("knowledge.detail.contentTypeImage"), icon: ImageIcon, count: contentTypeCounts.image },
        ].map((tab) => (
          <button
            key={tab.key}
            onClick={() => setContentTypeFilter(tab.key)}
            className={`
              flex items-center gap-1.5 px-4 py-1.5 rounded-full text-sm transition-[background-color,color,box-shadow] duration-150
              ${contentTypeFilter === tab.key
                ? "bg-background shadow-xs text-foreground font-medium"
                : "text-muted-foreground hover:text-foreground"
              }
            `}
          >
            <tab.icon className="h-4 w-4" />
            {tab.label}
            {tab.count > 0 && (
              <span className={`text-xs ${contentTypeFilter === tab.key ? "text-primary" : "text-muted-foreground"}`}>
                {tab.count}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* 工具栏 - 阿里云风格 */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="grid grid-cols-[112px_minmax(0,1fr)] items-center gap-2 sm:flex sm:gap-3">
          {/* 筛选下拉 */}
          <Select value={searchField} onValueChange={(v) => setSearchField(v as "name" | "id")}>
            <SelectTrigger className="w-28 bg-card h-9">
              <SelectValue placeholder={t("knowledge.detail.searchByName")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="name">{t("knowledge.detail.searchByName")}</SelectItem>
              <SelectItem value="id">{t("knowledge.detail.searchByID")}</SelectItem>
            </SelectContent>
          </Select>

          {/* 搜索框 */}
          <div className="relative">
            <Input
              placeholder={searchField === "name" ? t("knowledge.detail.searchNamePlaceholder") : t("knowledge.detail.searchIdPlaceholder")}
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              className="h-9 w-full bg-card pr-8 sm:w-64"
            />
            <Search className="absolute right-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground/70" />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Select value={statusFilter} onValueChange={(v) => setStatusFilter(v as typeof statusFilter)}>
            <SelectTrigger className="w-28 bg-card h-9">
              <SelectValue placeholder={t("knowledge.detail.allStatus")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t("knowledge.detail.allStatus")}</SelectItem>
              <SelectItem value="completed">{t("knowledge.detail.statusCompleted")}</SelectItem>
              <SelectItem value="uploaded">{t("knowledge.detail.statusUploaded")}</SelectItem>
              <SelectItem value="processing">{t("knowledge.detail.statusProcessing")}</SelectItem>
              <SelectItem value="failed">{t("knowledge.detail.statusFailed")}</SelectItem>
            </SelectContent>
          </Select>
          <Select value={formatFilter} onValueChange={setFormatFilter}>
            <SelectTrigger className="w-32 bg-card h-9">
              <SelectValue placeholder={t("knowledge.detail.allFormats")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t("knowledge.detail.allFormats")}</SelectItem>
              <SelectItem value="pdf">PDF</SelectItem>
              <SelectItem value="docx">Word</SelectItem>
              <SelectItem value="txt">TXT</SelectItem>
              <SelectItem value="md">Markdown</SelectItem>
            </SelectContent>
          </Select>
          <Button
            variant="outline"
            size="icon"
            className="h-9 w-9 bg-card"
            onClick={() => qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] })}
          >
            <RefreshCcw className={`h-4 w-4 ${docsQuery.isFetching ? "animate-spin" : ""}`} />
          </Button>
          {/* 批量操作下拉菜单 */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant={batchMode ? "default" : "outline-solid"}
                className={`h-9 ${batchMode ? "bg-primary text-white" : "bg-card"}`}
              >
                <ListChecks className="h-4 w-4 mr-1.5" />
                {t("knowledge.detail.batchOperations")}
                {selectedDocIds.size > 0 && (
                  <Badge variant="secondary" className="ml-1.5 h-5 px-1.5 text-xs">
                    {selectedDocIds.size}
                  </Badge>
                )}
                <ChevronDown className="h-4 w-4 ml-1" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-48">
              <DropdownMenuItem onClick={toggleBatchMode}>
                {batchMode ? (
                  <>
                    <X className="h-4 w-4 mr-2" />
                    {t("knowledge.detail.exitBatchMode")}
                  </>
                ) : (
                  <>
                    <CheckSquare className="h-4 w-4 mr-2" />
                    {t("knowledge.detail.enterBatchMode")}
                  </>
                )}
              </DropdownMenuItem>
              {batchMode && (
                <>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onClick={toggleSelectAll}>
                    {selectedDocIds.size === filteredDocs.length ? (
                      <>
                        <Square className="h-4 w-4 mr-2" />
                        {t("knowledge.detail.deselectAll")}
                      </>
                    ) : (
                      <>
                        <CheckSquare className="h-4 w-4 mr-2" />
                        {t("knowledge.detail.selectAll")}
                      </>
                    )}
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={() => selectByStatus("uploaded")}>
                    <Upload className="h-4 w-4 mr-2" />
                    {t("knowledge.detail.selectUploaded")}
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={() => selectByStatus("failed")}>
                    <X className="h-4 w-4 mr-2" />
                    {t("knowledge.detail.selectFailed")}
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    onClick={() => setBatchReindexOpen(true)}
                    disabled={selectedDocIds.size === 0}
                    className="text-primary"
                  >
                    <Zap className="h-4 w-4 mr-2" />
                    {t("knowledge.detail.batchReindex", { count: selectedDocIds.size })}
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onClick={() => setBatchDeleteOpen(true)}
                    disabled={selectedDocIds.size === 0}
                    className="text-destructive"
                  >
                    <Trash2 className="h-4 w-4 mr-2" />
                    {t("knowledge.detail.batchDelete", { count: selectedDocIds.size })}
                  </DropdownMenuItem>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>

          {/* 添加数据下拉菜单 */}
          <input
            ref={fileInputRef}
            type="file"
            className="hidden"
            accept=".txt,.md,.pdf,.docx,.html"
            multiple
            onChange={(e) => onFilesSelected(e.target.files)}
          />
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                disabled={uploading}
                className="h-9 bg-primary hover:bg-primary/90 text-white"
              >
                <Plus className="h-4 w-4 mr-1.5" />
                {t("knowledge.detail.addData")}
                <ChevronDown className="h-4 w-4 ml-1" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onClick={openFilePicker}>
                <Upload className="h-4 w-4 mr-2" />
                {t("knowledge.detail.uploadFile")}
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={onOpenUrlDialog}>
                <Globe className="h-4 w-4 mr-2" />
                {t("knowledge.detail.addUrl")}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={onOpenTextDialog}>
                <FileText className="h-4 w-4 mr-2" />
                {t("knowledge.detail.addText")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      {/* 文档列表 - 表格形式 */}
      <Card className="p-0 overflow-hidden border-border">
        {/* 表头 */}
        <div className="hidden items-center px-5 py-3 bg-muted/40 border-b border-border text-sm font-medium text-muted-foreground sm:flex">
          {batchMode && (
            <div className="mr-3 flex items-center">
              <Checkbox
                checked={filteredDocs.length > 0 && selectedDocIds.size === filteredDocs.length}
                onCheckedChange={toggleSelectAll}
              />
            </div>
          )}
          <div className="flex-1">{t("knowledge.detail.headerName")}</div>
          <div className="w-24 text-center">{t("knowledge.detail.headerSize")}</div>
          <div className="w-28 text-center">{t("knowledge.detail.headerStatus")}</div>
          <div className="w-28 text-center">{t("knowledge.detail.headerCategory")}</div>
          <div className="w-40 text-center">{t("knowledge.detail.headerUploadTime")}</div>
          <div className="w-48 text-center">{t("knowledge.detail.headerActions")}</div>
        </div>

        {/* 文档列表 */}
        <div className="max-h-[300px] overflow-auto">
          {filteredDocs.map((doc) => (
            <DocumentRow
              key={doc.document_id}
              doc={doc}
              datasetId={datasetId || ""}
              selected={selectedDocId === doc.document_id}
              checked={selectedDocIds.has(doc.document_id)}
              showCheckbox={batchMode}
              onSelect={() => setSelectedDocId(doc.document_id)}
              onCheck={() => toggleDocSelection(doc.document_id)}
              onReindex={() => handleReindex(doc)}
              onDelete={() => handleDeleteDoc(doc)}
              onVersionRestored={() => qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] })}
            />
          ))}
          {filteredDocs.length === 0 && !docsQuery.isLoading && (
            <div className="text-center py-16">
              <FileText className="h-12 w-12 mx-auto text-muted-foreground/50 mb-3" />
              <p className="text-muted-foreground">{t("knowledge.detail.noDocuments")}</p>
              <p className="text-sm text-muted-foreground/70 mt-1">{t("knowledge.detail.noDocumentsHint")}</p>
            </div>
          )}
        </div>
      </Card>

      {/* 切片列表 */}
      {selectedDoc && (
        <Card className="p-0 overflow-hidden shadow-xs border-primary/20 mt-6">
          {/* 标题栏 - 带明显的返回按钮 */}
          <div className="px-5 py-4 border-b border-border bg-linear-to-r from-muted/70 via-card to-primary/10 flex items-center justify-between">
            <div className="flex items-center gap-4">
              <button
                onClick={() => setSelectedDocId(undefined)}
                className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-muted-foreground hover:text-primary bg-card hover:bg-primary/5 border border-border hover:border-primary/20 rounded-lg transition-[color,background-color,border-color] duration-150 shadow-xs"
              >
                <ArrowLeft className="h-4 w-4" />
                <span>{t("knowledge.detail.backToList")}</span>
              </button>
              <div className="w-px h-6 bg-border" />
              <div className="flex items-center gap-3">
                <div className="w-8 h-8 rounded-lg bg-primary/10 border border-primary/20 flex items-center justify-center">
                  <Hash className="h-4 w-4 text-primary" />
                </div>
                <div>
                  <h3 className="font-semibold text-foreground">{t("knowledge.detail.segmentList")}</h3>
                  <p className="text-xs text-muted-foreground">{selectedDoc.title}</p>
                </div>
              </div>
              <Badge className="bg-primary/10 text-primary/90 border-primary/20">{t("knowledge.detail.segmentCount", { count: segments.length })}</Badge>
            </div>
            <div className="flex items-center gap-3">
              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground/70" />
                <Input
                  placeholder={t("knowledge.detail.searchSegments")}
                  value={segmentSearch}
                  onChange={(e) => setSegmentSearch(e.target.value)}
                  className="pl-9 w-56 h-9 text-sm bg-card border-border"
                />
              </div>
            </div>
          </div>

          {/* 切片网格 */}
          <div className="max-h-[500px] overflow-auto p-4 bg-muted/30">
            {segments.length === 0 ? (
              <div className="text-center py-16">
                <div className="w-16 h-16 mx-auto rounded-2xl bg-linear-to-br from-muted/40 to-muted flex items-center justify-center mb-4">
                  <Hash className="h-8 w-8 text-muted-foreground/70" />
                </div>
                <p className="text-muted-foreground font-medium">{t("knowledge.detail.noSegments")}</p>
                <p className="text-sm text-muted-foreground/70 mt-1">{t("knowledge.detail.noSegmentsHint")}</p>
              </div>
            ) : (
              <SegmentList
                segments={segments}
                onEdit={(id, text) => openEdit(id, text)}
                onDelete={(id) => handleDeleteSegment(id)}
              />
            )}
          </div>
        </Card>
      )}

      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t("knowledge.detail.editSegment")}</DialogTitle>
          </DialogHeader>
          <Textarea value={editText} onChange={(e) => setEditText(e.target.value)} rows={12} />
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditOpen(false)}>
              {t("knowledge.detail.cancel")}
            </Button>
            <Button onClick={saveEdit} disabled={editSaving} className="bg-primary hover:bg-primary/90">
              {editSaving ? t("knowledge.detail.saving") : t("knowledge.detail.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 批量重建索引确认对话框 */}
      <Dialog open={batchReindexOpen} onOpenChange={setBatchReindexOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Zap className="h-5 w-5 text-primary" />
              {t("knowledge.detail.batchReindexTitle")}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              {t("knowledge.detail.batchReindexText", { count: selectedDocIds.size })}
            </p>
            <div className="bg-muted/50 rounded-lg p-3 text-xs text-muted-foreground space-y-1">
              <p>• {t("knowledge.detail.batchReindexHint1")}</p>
              <p>• {t("knowledge.detail.batchReindexHint2")}</p>
              <p>• {t("knowledge.detail.batchReindexHint3")}</p>
            </div>
            {/* 选中的文档列表预览 */}
            <div className="max-h-32 overflow-auto border border-border rounded-lg">
              {Array.from(selectedDocIds).slice(0, 10).map((docId) => {
                const doc = docs.find((d) => d.document_id === docId);
                return doc ? (
                  <div key={docId} className="px-3 py-1.5 text-sm border-b border-border/50 last:border-b-0 truncate">
                    {doc.title}
                  </div>
                ) : null;
              })}
              {selectedDocIds.size > 10 && (
                <div className="px-3 py-1.5 text-sm text-muted-foreground">
                  {t("knowledge.detail.moreDocuments", { count: selectedDocIds.size - 10 })}
                </div>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setBatchReindexOpen(false)} disabled={batchLoading}>
              {t("knowledge.detail.cancel")}
            </Button>
            <Button onClick={handleBatchReindex} disabled={batchLoading} className="bg-primary hover:bg-primary/90">
              {batchLoading ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  {t("knowledge.detail.processing")}
                </>
              ) : (
                t("knowledge.detail.confirmReindex", { count: selectedDocIds.size })
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 批量删除确认对话框 */}
      <Dialog open={batchDeleteOpen} onOpenChange={setBatchDeleteOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-destructive">
              <Trash2 className="h-5 w-5" />
              {t("knowledge.detail.batchDeleteTitle")}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              {t("knowledge.detail.batchDeleteText", { count: selectedDocIds.size })}
            </p>
            <div className="bg-destructive/10 rounded-lg p-3 text-xs text-destructive space-y-1">
              <p>{t("knowledge.detail.batchDeleteWarning")}</p>
              <p>• {t("knowledge.detail.batchDeleteHint")}</p>
            </div>
            {/* 选中的文档列表预览 */}
            <div className="max-h-32 overflow-auto border border-border rounded-lg">
              {Array.from(selectedDocIds).slice(0, 10).map((docId) => {
                const doc = docs.find((d) => d.document_id === docId);
                return doc ? (
                  <div key={docId} className="px-3 py-1.5 text-sm border-b border-border/50 last:border-b-0 truncate">
                    {doc.title}
                  </div>
                ) : null;
              })}
              {selectedDocIds.size > 10 && (
                <div className="px-3 py-1.5 text-sm text-muted-foreground">
                  {t("knowledge.detail.moreDocuments", { count: selectedDocIds.size - 10 })}
                </div>
              )}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setBatchDeleteOpen(false)} disabled={batchLoading}>
              {t("knowledge.detail.cancel")}
            </Button>
            <Button variant="destructive" onClick={handleBatchDelete} disabled={batchLoading}>
              {batchLoading ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  {t("knowledge.detail.deleting")}
                </>
              ) : (
                t("knowledge.detail.confirmBatchDelete", { count: selectedDocIds.size })
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
