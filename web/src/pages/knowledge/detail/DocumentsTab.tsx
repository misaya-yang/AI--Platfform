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

import { useEffect, useMemo, useState, type RefObject } from "react";
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
  Archive,
  ArchiveRestore,
  ChevronLeft,
  ChevronRight,
  Tags,
} from "lucide-react";

import {
  useDebouncedValue,
  useDocumentProgressStream,
  useDocuments,
  useSegments,
} from "@/hooks/useKnowledge";
import {
  deleteDocument,
  deleteSegment,
  reembedDocument,
  reprocessDocument,
  recoverDocument,
  retryDocument,
  updateSegment,
  batchReindexDocuments,
  batchDeleteDocuments,
  setSegmentEnabled,
  batchSetSegmentsEnabled,
  setDocumentEnabled,
  setDocumentArchived,
  DOCUMENT_BATCH_REINDEX_LIMIT,
  DOCUMENT_BATCH_DELETE_LIMIT,
  DOCUMENT_ARCHIVE_REASON_LIMIT,
  waitForDocumentBatchOperation,
  batchUpdateDocumentMetadata,
  getDocumentMetadataRegistry,
  updateDocumentMetadata,
  type DocumentPipelineAction,
} from "@/api/knowledge";
import { partitionIds, summarizeDocumentBatches } from "./batchOperations";
import {
  DOCUMENT_DISPLAY_STATUS_VOCABULARY,
  documentNeedsLifecyclePolling,
  parseSegmentKeywords,
  resolveDisplayStatus,
  type Document,
  type DocumentDisplayStatus,
  type DocumentMetadataRegistry,
} from "@/types/knowledge";

import { Button } from "@/components/ui/button";
import { mergeDocumentPage } from "./documentCache";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
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
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
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
import { DocumentMetadataDialog } from "@/pages/knowledge/detail/DocumentMetadataDialog";

const FILE_TYPE_CATEGORIES: Record<string, string[]> = {
  document: ["pdf", "doc", "docx", "txt", "md", "html", "rtf"],
  data: ["xls", "xlsx", "csv", "json", "xml"],
  image: ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"],
};

// Backend SegmentUpdateSchema / SegmentBatchEnableDisableSchema limits
// (knowledge-service): at most 100 keywords of 1..256 chars each per
// segment, and 1..500 segment ids per batch enable/disable call.
const SEGMENT_KEYWORDS_LIMIT = 100;
const SEGMENT_KEYWORD_MAX_LENGTH = 256;
const SEGMENT_BATCH_LIMIT = 500;
const DOCUMENT_METADATA_BATCH_LIMIT = 500;
const EMPTY_METADATA_REGISTRY: DocumentMetadataRegistry = {
  version: 1,
  revision: 0,
  fields: [],
};

const DOCUMENT_PIPELINE_REQUESTS: Record<
  DocumentPipelineAction,
  (datasetId: string, documentId: string) => Promise<unknown>
> = {
  reembed: reembedDocument,
  reprocess: reprocessDocument,
  recover: recoverDocument,
  retry: retryDocument,
};

interface DocumentsTabProps {
  datasetId?: string;
  docs: Document[];
  docsQuery: ReturnType<typeof useDocuments>;
  totalDocuments: number;
  documentLimit: number;
  documentOffset: number;
  onDocumentOffsetChange: (offset: number) => void;
  permission?: string;
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
  totalDocuments,
  documentLimit,
  documentOffset,
  onDocumentOffsetChange,
  permission,
  fileInputRef,
  uploading,
  openFilePicker,
  onFilesSelected,
  onOpenTextDialog,
  onOpenUrlDialog,
}: DocumentsTabProps) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  useDocumentProgressStream(
    datasetId,
    docs.some(documentNeedsLifecyclePolling),
  );

  const [selectedDocId, setSelectedDocId] = useState<string | undefined>(undefined);
  const selectedDoc = useMemo(
    () => docs.find((d) => d.document_id === selectedDocId),
    [docs, selectedDocId]
  );

  const [segmentSearch, setSegmentSearch] = useState("");
  // Server-side segment search is debounced so keystrokes don't fan out
  // queries. The debounced value keys the query, so every manual
  // invalidation below must use the same variable.
  const debouncedSegmentSearch = useDebouncedValue(segmentSearch);
  const [segmentOffset, setSegmentOffset] = useState(0);
  const segmentLimit = 100;
  const segmentsQuery = useSegments(
    datasetId,
    selectedDocId,
    debouncedSegmentSearch,
    { limit: segmentLimit, offset: segmentOffset }
  );
  const segments = segmentsQuery.data?.items ?? [];
  const segmentTotal = segmentsQuery.data?.total ?? 0;

  useEffect(() => {
    setSegmentOffset(0);
  }, [selectedDocId, debouncedSegmentSearch]);

  useEffect(() => {
    if (totalDocuments > 0 && documentOffset >= totalDocuments) {
      onDocumentOffsetChange(
        Math.floor((totalDocuments - 1) / documentLimit) * documentLimit
      );
    }
  }, [documentLimit, documentOffset, onDocumentOffsetChange, totalDocuments]);

  // Segment edit dialog — full-field edit: text + answer + keywords are
  // loaded from the segment and saved together (PRD §5-#13).
  const [editOpen, setEditOpen] = useState(false);
  const [editSegmentId, setEditSegmentId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [editAnswer, setEditAnswer] = useState("");
  const [editKeywords, setEditKeywords] = useState("");
  const [editSaving, setEditSaving] = useState(false);

  // Segment batch operations (enable/disable many segments at once)
  const [segmentBatchMode, setSegmentBatchMode] = useState(false);
  const [selectedSegmentIds, setSelectedSegmentIds] = useState<Set<string>>(new Set());
  const [segmentBatchLoading, setSegmentBatchLoading] = useState(false);
  // Segments with an in-flight enable/disable mutation (single or batch):
  // their switches/checkboxes render disabled until the mutation settles.
  const [pendingSegmentIds, setPendingSegmentIds] = useState<Set<string>>(new Set());

  // Batch operations
  const [batchMode, setBatchMode] = useState(false);
  const [selectedDocIds, setSelectedDocIds] = useState<Set<string>>(new Set());
  const [batchReindexOpen, setBatchReindexOpen] = useState(false);
  const [batchReindexAll, setBatchReindexAll] = useState(false);
  const [batchDeleteOpen, setBatchDeleteOpen] = useState(false);
  const [batchLoading, setBatchLoading] = useState(false);
  // Ids the batch-reindex endpoint reported as skipped (409 all-skipped
  // contract): rendered inside the dialog so users can deselect and retry.
  const [batchReindexSkipped, setBatchReindexSkipped] = useState<string[] | null>(null);

  // Document lifecycle (enable/disable, archive): rows with an in-flight
  // mutation render their controls disabled until it settles.
  const [pendingDocIds, setPendingDocIds] = useState<Set<string>>(new Set());
  const [archiveOpen, setArchiveOpen] = useState(false);
  const [archiveDocId, setArchiveDocId] = useState<string | null>(null);
  const [archiveReason, setArchiveReason] = useState("");
  const [archiveSaving, setArchiveSaving] = useState(false);
  const [unarchiveDocId, setUnarchiveDocId] = useState<string | null>(null);
  const [metadataRegistry, setMetadataRegistry] = useState<DocumentMetadataRegistry>(
    EMPTY_METADATA_REGISTRY
  );
  const [metadataDocuments, setMetadataDocuments] = useState<Document[]>([]);
  const [metadataOpen, setMetadataOpen] = useState(false);
  const [metadataSaving, setMetadataSaving] = useState(false);
  const canEditMetadata = permission === "owner" || permission === "editor";

  // Status filtering runs on the display vocabulary, through the same
  // resolver the row badges use, so a filter always matches what users see.
  const [statusFilter, setStatusFilter] = useState<"all" | DocumentDisplayStatus>("all");
  const [contentTypeFilter, setContentTypeFilter] = useState<"all" | "document" | "data" | "image">("all");

  // Search state
  const [searchField, setSearchField] = useState<"name" | "id">("name");
  const [searchTerm, setSearchTerm] = useState("");
  // Debounce the client-side filter too: the input stays responsive while
  // large lists re-filter at most every 300ms.
  const debouncedSearchTerm = useDebouncedValue(searchTerm);
  const [formatFilter, setFormatFilter] = useState("all");

  // Filter documents by status, content type, format, and search term
  const filteredDocs = useMemo(() => {
    let result = docs;

    // Filter by search term
    if (debouncedSearchTerm.trim()) {
      const term = debouncedSearchTerm.toLowerCase().trim();
      result = result.filter((d) => {
        if (searchField === "name") {
          return d.title?.toLowerCase().includes(term);
        } else {
          return d.document_id?.toLowerCase().includes(term);
        }
      });
    }

    // Filter by display status (same resolution the row badges render).
    if (statusFilter !== "all") {
      result = result.filter((d) => resolveDisplayStatus(d) === statusFilter);
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
  }, [docs, debouncedSearchTerm, searchField, statusFilter, formatFilter, contentTypeFilter]);

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

  async function handleDocumentPipelineAction(
    doc: Document,
    action: DocumentPipelineAction
  ) {
    if (!datasetId) return;

    try {
      await DOCUMENT_PIPELINE_REQUESTS[action](datasetId, doc.document_id);
      const actionLabel = t(`knowledge.documentRow.${action}Title`);
      toast.success(
        t("knowledge.detail.documentActionQueuedTitle", { action: actionLabel }),
        t("knowledge.detail.documentActionQueuedDesc", { title: doc.title })
      );
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
    } catch (e) {
      // Every pipeline verb uses the same durable single-owner queue. A 409
      // means this document already has a generation owner, not that the
      // user's earlier submission was lost.
      const status = (e as { response?: { status?: number } } | null)?.response?.status;
      if (status === 409) {
        toast.warning(
          t("knowledge.detail.reindexQueuedTitle"),
          t("knowledge.detail.reindexQueuedText", { title: doc.title })
        );
        await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
        return;
      }
      console.error(`Document ${action} failed:`, e);
      toast.error(
        t("knowledge.detail.documentActionFailed", {
          action: t(`knowledge.documentRow.${action}Title`),
        }),
        e instanceof Error ? e.message : String(e)
      );
    }
  }

  async function handleDeleteDoc(doc: Document) {
    if (!datasetId) return;
    await deleteDocument(datasetId, doc.document_id);
    await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
    if (selectedDocId === doc.document_id) setSelectedDocId(undefined);
  }

  // ---- Document lifecycle (enable/disable, archive) ----

  /**
   * Mutation responses carry the full document (every column + a fresh
   * display_status stamp), so merge them straight into the cached list: the
   * row reflects the new state immediately even before the refetch lands
   * (dependency D1: the list SELECT does not return enabled/archived yet, so
   * the refetch alone would not).
   */
  function mergeDocumentIntoCache(updated: Document) {
    if (!datasetId) return;
    qc.setQueriesData({ queryKey: ["kb-documents", datasetId] }, mergeDocumentPage(updated));
  }

  async function handleToggleDocEnabled(doc: Document, enabled: boolean) {
    if (!datasetId) return;
    setPendingDocIds((prev) => new Set(prev).add(doc.document_id));
    try {
      const updated = await setDocumentEnabled(datasetId, doc.document_id, enabled);
      mergeDocumentIntoCache(updated);
      toast.success(
        enabled
          ? t("knowledge.detail.documentEnableQueued")
          : t("knowledge.detail.documentDisabled"),
        enabled
          ? t("knowledge.detail.documentEnableQueuedDesc")
          : t("knowledge.detail.documentDisabledDesc")
      );
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
    } catch (e) {
      console.error("Document status update failed:", e);
      toast.error(
        t("knowledge.detail.documentStatusFailed"),
        e instanceof Error ? e.message : String(e)
      );
    } finally {
      setPendingDocIds((prev) => {
        const next = new Set(prev);
        next.delete(doc.document_id);
        return next;
      });
    }
  }

  function openArchiveDialog(doc: Document) {
    setArchiveDocId(doc.document_id);
    setArchiveReason("");
    setArchiveOpen(true);
  }

  async function confirmArchive() {
    if (!datasetId || !archiveDocId) return;
    const reason = archiveReason.trim();
    if (reason.length > DOCUMENT_ARCHIVE_REASON_LIMIT) {
      toast.error(t("knowledge.detail.archiveReasonTooLong"));
      return;
    }
    const doc = docs.find((d) => d.document_id === archiveDocId);
    setArchiveSaving(true);
    try {
      const updated = await setDocumentArchived(
        datasetId,
        archiveDocId,
        true,
        reason || undefined
      );
      mergeDocumentIntoCache(updated);
      setArchiveOpen(false);
      toast.success(
        t("knowledge.detail.archiveSuccess"),
        t("knowledge.detail.archiveSuccessDesc", { title: doc?.title ?? "" })
      );
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
    } catch (e) {
      console.error("Archive failed:", e);
      toast.error(
        t("knowledge.detail.archiveFailed"),
        e instanceof Error ? e.message : String(e)
      );
    } finally {
      setArchiveSaving(false);
    }
  }

  async function confirmUnarchive() {
    if (!datasetId || !unarchiveDocId) return;
    const doc = docs.find((d) => d.document_id === unarchiveDocId);
    setPendingDocIds((prev) => new Set(prev).add(unarchiveDocId));
    try {
      const updated = await setDocumentArchived(datasetId, unarchiveDocId, false);
      mergeDocumentIntoCache(updated);
      setUnarchiveDocId(null);
      toast.success(
        t("knowledge.detail.unarchiveQueued"),
        t("knowledge.detail.unarchiveQueuedDesc", { title: doc?.title ?? "" })
      );
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
    } catch (e) {
      console.error("Unarchive failed:", e);
      toast.error(
        t("knowledge.detail.unarchiveFailed"),
        e instanceof Error ? e.message : String(e)
      );
    } finally {
      setPendingDocIds((prev) => {
        const next = new Set(prev);
        if (unarchiveDocId) next.delete(unarchiveDocId);
        return next;
      });
    }
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
    const pageIds = filteredDocs.map((document) => document.document_id);
    const allSelected = pageIds.length > 0 && pageIds.every((id) => selectedDocIds.has(id));
    setSelectedDocIds((previous) => {
      const next = new Set(previous);
      for (const id of pageIds) {
        if (allSelected) next.delete(id);
        else next.add(id);
      }
      return next;
    });
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

  async function openMetadataEditor(documentIds: Iterable<string>) {
    if (!datasetId) return;
    try {
      setMetadataRegistry(await getDocumentMetadataRegistry(datasetId));
    } catch (error) {
      toast.error(
        t("knowledge.metadata.loadFailed"),
        error instanceof Error ? error.message : String(error)
      );
      return;
    }
    const targets = Array.from(documentIds).map(
      (documentId) =>
        docs.find((document) => document.document_id === documentId) ?? {
          document_id: documentId,
          dataset_id: datasetId ?? "",
          title: documentId,
          status: "completed" as const,
        }
    );
    setMetadataDocuments(targets);
    setMetadataOpen(true);
  }

  async function saveMetadata(
    metadataPatch: Record<string, unknown>,
    metadataRemove: string[]
  ) {
    if (!datasetId || metadataDocuments.length === 0) return;
    setMetadataSaving(true);
    try {
      if (metadataDocuments.length === 1) {
        await updateDocumentMetadata(
          datasetId,
          metadataDocuments[0].document_id,
          {
            metadataPatch,
            metadataRemove,
            metadataSchemaRevision: metadataRegistry.revision,
          }
        );
      } else {
        for (const ids of partitionIds(
          metadataDocuments.map((document) => document.document_id),
          DOCUMENT_METADATA_BATCH_LIMIT
        )) {
          await batchUpdateDocumentMetadata(datasetId, ids, {
            metadataPatch,
            metadataRemove,
            metadataSchemaRevision: metadataRegistry.revision,
          });
        }
      }
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      setMetadataOpen(false);
      toast.success(t("knowledge.metadata.saved"));
    } catch (error) {
      const status = (error as { response?: { status?: number } } | null)?.response?.status;
      if (status === 409) {
        const latest = await getDocumentMetadataRegistry(datasetId);
        setMetadataRegistry(latest);
        toast.warning(
          t("knowledge.metadata.schemaChanged"),
          t("knowledge.metadata.schemaChangedHint")
        );
      } else {
        toast.error(
          t("knowledge.metadata.saveFailed"),
          error instanceof Error ? error.message : String(error)
        );
      }
    } finally {
      setMetadataSaving(false);
    }
  }

  async function handleBatchReindex() {
    if (!datasetId || (!batchReindexAll && selectedDocIds.size === 0)) return;
    setBatchLoading(true);
    try {
      const submitted = batchReindexAll
        ? [await batchReindexDocuments(datasetId, [], true)]
        : await Promise.all(
            partitionIds(selectedDocIds, DOCUMENT_BATCH_REINDEX_LIMIT).map((ids) =>
              batchReindexDocuments(datasetId, ids)
            )
          );
      const completed = await Promise.all(
        submitted.map((operation) =>
          waitForDocumentBatchOperation(datasetId, operation.operation_id)
        )
      );
      const summary = summarizeDocumentBatches(completed);
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      const skippedIds = completed.flatMap((operation) =>
        operation.problem_items
          .filter((item) => item.status === "skipped")
          .map((item) => item.document_id)
      );
      if (summary.skipped > 0 || summary.failed > 0) {
        setBatchReindexSkipped(skippedIds);
        if (summary.succeeded === 0) {
          toast.warning(
            t("knowledge.detail.batchReindexAllSkippedTitle"),
            t("knowledge.detail.batchReindexAllSkippedText")
          );
          return;
        }
        toast.warning(
          t("knowledge.detail.batchReindexDone"),
          t("knowledge.detail.batchReindexPartial", {
            queued: summary.succeeded,
            skipped: summary.skipped + summary.failed,
          })
        );
      } else {
        toast.success(
          t("knowledge.detail.batchReindexDone"),
          t("knowledge.detail.batchReindexSuccess", { count: summary.succeeded })
        );
      }
      setBatchReindexOpen(false);
      setBatchReindexSkipped(null);
      setBatchReindexAll(false);
      setSelectedDocIds(new Set());
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
      const submitted = await Promise.all(
        partitionIds(selectedDocIds, DOCUMENT_BATCH_DELETE_LIMIT).map((ids) =>
          batchDeleteDocuments(datasetId, ids)
        )
      );
      const completed = await Promise.all(
        submitted.map((operation) =>
          waitForDocumentBatchOperation(datasetId, operation.operation_id)
        )
      );
      const result = summarizeDocumentBatches(completed);
      await qc.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
      setBatchDeleteOpen(false);
      setSelectedDocIds(new Set());
      // Clear selected doc if it was deleted
      if (selectedDocId && selectedDocIds.has(selectedDocId)) {
        setSelectedDocId(undefined);
      }
      // Show result
      if (result.failed > 0 || result.skipped > 0) {
        toast.warning(
          t("knowledge.detail.batchDeleteDone"),
          `${result.succeeded} / ${result.failed + result.skipped}`
        );
      } else {
        toast.success(
          t("knowledge.detail.batchDeleteDone"),
          t("knowledge.detail.batchDeleteSuccess", { count: result.succeeded })
        );
      }
    } catch (e) {
      console.error("Batch delete failed:", e);
      toast.error(t("knowledge.detail.batchDeleteFailed"), e instanceof Error ? e.message : String(e));
    } finally {
      setBatchLoading(false);
    }
  }

  // Selecting another document (or leaving the panel) drops segment-level
  // batch state: it belongs to the previously open document's segment list.
  function selectDocument(docId: string | undefined) {
    setSelectedDocId(docId);
    setSegmentOffset(0);
    setSelectedSegmentIds(new Set());
    setSegmentBatchMode(false);
  }

  function openEdit(segmentId: string) {
    // Read every editable field from the loaded segment so the save below
    // round-trips them all (the old text-only edit silently dropped
    // answer/keywords — PRD §5-#13).
    const segment = segments.find((s) => s.segment_id === segmentId);
    setEditSegmentId(segmentId);
    setEditText(segment?.text ?? "");
    setEditAnswer(segment?.answer ?? "");
    setEditKeywords((segment?.keywords ?? []).join(", "));
    setEditOpen(true);
  }

  async function saveEdit() {
    if (!datasetId || !editSegmentId) return;
    const keywords = parseSegmentKeywords(editKeywords);
    if (keywords.length > SEGMENT_KEYWORDS_LIMIT) {
      toast.error(t("knowledge.segment.keywordsTooMany"));
      return;
    }
    if (keywords.some((keyword) => keyword.length > SEGMENT_KEYWORD_MAX_LENGTH)) {
      toast.error(t("knowledge.segment.keywordTooLong"));
      return;
    }
    setEditSaving(true);
    try {
      await updateSegment(datasetId, editSegmentId, {
        text: editText,
        answer: editAnswer,
        keywords,
      });
      await qc.invalidateQueries({
        queryKey: ["kb-segments", datasetId, selectedDocId, debouncedSegmentSearch],
      });
      toast.success(t("knowledge.segment.editSegmentSuccess"));
      setEditOpen(false);
    } catch (e) {
      console.error("Segment update failed:", e);
      toast.error(
        t("knowledge.segment.editSegmentFailed"),
        e instanceof Error ? e.message : String(e)
      );
    } finally {
      setEditSaving(false);
    }
  }

  async function handleDeleteSegment(segmentId: string) {
    if (!datasetId) return;
    await deleteSegment(datasetId, segmentId);
    await qc.invalidateQueries({
      queryKey: ["kb-segments", datasetId, selectedDocId, debouncedSegmentSearch],
    });
  }

  async function handleToggleSegmentEnabled(segmentId: string, enabled: boolean) {
    if (!datasetId) return;
    setPendingSegmentIds((prev) => new Set(prev).add(segmentId));
    try {
      await setSegmentEnabled(datasetId, segmentId, enabled);
      toast.success(
        enabled
          ? t("knowledge.segment.segmentEnabled")
          : t("knowledge.segment.segmentDisabled"),
        enabled
          ? t("knowledge.segment.segmentEnabledDesc")
          : t("knowledge.segment.segmentDisabledDesc")
      );
      await qc.invalidateQueries({
        queryKey: ["kb-segments", datasetId, selectedDocId, debouncedSegmentSearch],
      });
    } catch (e) {
      console.error("Segment status update failed:", e);
      toast.error(
        t("knowledge.segment.segmentStatusFailed"),
        e instanceof Error ? e.message : String(e)
      );
    } finally {
      setPendingSegmentIds((prev) => {
        const next = new Set(prev);
        next.delete(segmentId);
        return next;
      });
    }
  }

  function toggleSegmentBatchMode() {
    if (segmentBatchMode) {
      setSegmentBatchMode(false);
      setSelectedSegmentIds(new Set());
    } else {
      setSegmentBatchMode(true);
    }
  }

  function toggleSegmentSelect(segmentId: string) {
    setSelectedSegmentIds((prev) => {
      const next = new Set(prev);
      if (next.has(segmentId)) {
        next.delete(segmentId);
      } else {
        next.add(segmentId);
      }
      return next;
    });
  }

  function toggleSelectAllSegments() {
    const pageIds = segments.map((segment) => segment.segment_id);
    const allSelected = pageIds.length > 0 && pageIds.every((id) => selectedSegmentIds.has(id));
    setSelectedSegmentIds((previous) => {
      const next = new Set(previous);
      for (const id of pageIds) {
        if (allSelected) next.delete(id);
        else next.add(id);
      }
      return next;
    });
  }

  async function handleSegmentBatchEnable(enabled: boolean) {
    if (!datasetId || selectedSegmentIds.size === 0) return;
    const ids = Array.from(selectedSegmentIds);
    setSegmentBatchLoading(true);
    setPendingSegmentIds((prev) => {
      const next = new Set(prev);
      ids.forEach((id) => next.add(id));
      return next;
    });
    try {
      const results = [];
      for (const chunk of partitionIds(ids, SEGMENT_BATCH_LIMIT)) {
        results.push(await batchSetSegmentsEnabled(datasetId, chunk, enabled));
      }
      const result = results.reduce(
        (summary, item) => ({
          updated: summary.updated + item.updated,
          total: summary.total + item.total,
        }),
        { updated: 0, total: 0 }
      );
      await qc.invalidateQueries({
        queryKey: ["kb-segments", datasetId, selectedDocId, debouncedSegmentSearch],
      });
      setSelectedSegmentIds(new Set());
      setSegmentBatchMode(false);
      // The endpoint skips per-item failures, so report exactly how many
      // rows changed instead of assuming all-or-nothing.
      if (result.updated < result.total) {
        toast.warning(
          t("knowledge.segment.batchDone"),
          t("knowledge.segment.batchPartial", { updated: result.updated, total: result.total })
        );
      } else {
        toast.success(
          t("knowledge.segment.batchDone"),
          t("knowledge.segment.batchSuccess", { count: result.updated })
        );
      }
    } catch (e) {
      console.error("Segment batch update failed:", e);
      toast.error(
        t("knowledge.segment.batchFailed"),
        e instanceof Error ? e.message : String(e)
      );
    } finally {
      setSegmentBatchLoading(false);
      setPendingSegmentIds((prev) => {
        const next = new Set(prev);
        ids.forEach((id) => next.delete(id));
        return next;
      });
    }
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
            <SelectTrigger data-testid="doc-status-filter" className="w-28 bg-card h-9">
              <SelectValue placeholder={t("knowledge.detail.allStatus")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t("knowledge.detail.allStatus")}</SelectItem>
              {DOCUMENT_DISPLAY_STATUS_VOCABULARY.map((value) => (
                <SelectItem key={value} value={value}>
                  {t(`knowledge.displayStatus.${value}`)}
                </SelectItem>
              ))}
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
                variant={batchMode ? "default" : "outline"}
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
                    {filteredDocs.length > 0 && filteredDocs.every((doc) => selectedDocIds.has(doc.document_id)) ? (
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
                    onClick={() => void openMetadataEditor(selectedDocIds)}
                    disabled={
                      !canEditMetadata ||
                      selectedDocIds.size === 0
                    }
                  >
                    <Tags className="h-4 w-4 mr-2" />
                    {t("knowledge.metadata.editBatch", { count: selectedDocIds.size })}
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onClick={() => {
                      setBatchReindexSkipped(null);
                      setBatchReindexAll(false);
                      setBatchReindexOpen(true);
                    }}
                    disabled={selectedDocIds.size === 0}
                    className="text-primary"
                  >
                    <Zap className="h-4 w-4 mr-2" />
                    {t("knowledge.detail.batchReindex", { count: selectedDocIds.size })}
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onClick={() => {
                      setBatchReindexSkipped(null);
                      setBatchReindexAll(true);
                      setBatchReindexOpen(true);
                    }}
                    disabled={totalDocuments === 0}
                    className="text-primary"
                  >
                    <Zap className="h-4 w-4 mr-2" />
                    {t("knowledge.detail.batchReindexAll", { count: totalDocuments })}
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
                checked={
                  filteredDocs.length > 0 &&
                  filteredDocs.every((doc) => selectedDocIds.has(doc.document_id))
                }
                onCheckedChange={toggleSelectAll}
              />
            </div>
          )}
          <div className="flex-1">{t("knowledge.detail.headerName")}</div>
          <div className="w-24 text-center">{t("knowledge.detail.headerSize")}</div>
          <div className="w-28 text-center">{t("knowledge.detail.headerStatus")}</div>
          <div className="w-28 text-center">{t("knowledge.detail.headerCategory")}</div>
          <div className="w-40 text-center">{t("knowledge.detail.headerUploadTime")}</div>
          <div className="w-64 text-center">{t("knowledge.detail.headerActions")}</div>
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
              onSelect={() => selectDocument(doc.document_id)}
              onCheck={() => toggleDocSelection(doc.document_id)}
              onReembed={() => handleDocumentPipelineAction(doc, "reembed")}
              onReprocess={() => handleDocumentPipelineAction(doc, "reprocess")}
              onRecover={() => handleDocumentPipelineAction(doc, "recover")}
              onRetry={() => handleDocumentPipelineAction(doc, "retry")}
              onDelete={() => handleDeleteDoc(doc)}
              onToggleEnabled={(enabled) => handleToggleDocEnabled(doc, enabled)}
              onArchive={() => openArchiveDialog(doc)}
              onUnarchive={() => setUnarchiveDocId(doc.document_id)}
              onEditMetadata={
                canEditMetadata
                  ? () => void openMetadataEditor([doc.document_id])
                  : undefined
              }
              busyLifecycle={pendingDocIds.has(doc.document_id)}
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

      <div
        data-testid="document-pagination"
        className="flex items-center justify-between text-sm text-muted-foreground"
      >
        <span>
          {t("knowledge.detail.paginationShowing", {
            start: totalDocuments === 0 ? 0 : documentOffset + 1,
            end: Math.min(documentOffset + docs.length, totalDocuments),
            total: totalDocuments,
          })}
        </span>
        <div className="flex items-center gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={documentOffset === 0 || docsQuery.isFetching}
            onClick={() => onDocumentOffsetChange(Math.max(0, documentOffset - documentLimit))}
          >
            <ChevronLeft className="mr-1 h-4 w-4" />
            {t("knowledge.detail.previousPage")}
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={documentOffset + documentLimit >= totalDocuments || docsQuery.isFetching}
            onClick={() => onDocumentOffsetChange(documentOffset + documentLimit)}
          >
            {t("knowledge.detail.nextPage")}
            <ChevronRight className="ml-1 h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* 切片列表 */}
      {selectedDoc && (
        <Card className="p-0 overflow-hidden shadow-xs border-primary/20 mt-6">
          {/* 标题栏 - 带明显的返回按钮 */}
          <div className="px-5 py-4 border-b border-border bg-linear-to-r from-muted/70 via-card to-primary/10 flex items-center justify-between">
            <div className="flex items-center gap-4">
              <button
                onClick={() => selectDocument(undefined)}
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
              <Badge className="bg-primary/10 text-primary/90 border-primary/20">{t("knowledge.detail.segmentCount", { count: segmentTotal })}</Badge>
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
              <Button
                data-testid="segment-batch-toggle"
                variant={segmentBatchMode ? "default" : "outline"}
                size="icon"
                className={`h-9 w-9 ${segmentBatchMode ? "bg-primary text-white hover:bg-primary/90" : "bg-card"}`}
                onClick={toggleSegmentBatchMode}
                title={t("knowledge.detail.batchOperations")}
                aria-label={t("knowledge.detail.batchOperations")}
                aria-pressed={segmentBatchMode}
              >
                <ListChecks className="h-4 w-4" />
              </Button>
            </div>
          </div>

          {/* 段落批量操作栏 */}
          {segmentBatchMode && segments.length > 0 && (
            <div
              data-testid="segment-batch-bar"
              className="flex flex-wrap items-center gap-3 px-5 py-2.5 border-b border-border bg-muted/40"
            >
              <div className="flex items-center gap-2">
                <Checkbox
                  checked={
                    segments.length > 0 &&
                    segments.every((segment) => selectedSegmentIds.has(segment.segment_id))
                  }
                  onCheckedChange={toggleSelectAllSegments}
                  aria-label={t("knowledge.detail.selectAll")}
                />
                <button
                  type="button"
                  onClick={toggleSelectAllSegments}
                  className="text-sm text-muted-foreground hover:text-foreground"
                >
                  {t("knowledge.detail.selectAll")}
                </button>
              </div>
              <span
                data-testid="segment-batch-count"
                className="text-sm text-muted-foreground"
              >
                {t("knowledge.segment.selectedCount", { count: selectedSegmentIds.size })}
              </span>
              <div className="ml-auto flex items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  className="h-8 bg-card"
                  disabled={selectedSegmentIds.size === 0 || segmentBatchLoading}
                  onClick={() => handleSegmentBatchEnable(true)}
                >
                  {t("knowledge.segment.enable")}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="h-8 bg-card"
                  disabled={selectedSegmentIds.size === 0 || segmentBatchLoading}
                  onClick={() => handleSegmentBatchEnable(false)}
                >
                  {t("knowledge.segment.disable")}
                </Button>
              </div>
            </div>
          )}

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
                partialPage={segmentTotal > segments.length}
                onEdit={(id) => openEdit(id)}
                onDelete={(id) => handleDeleteSegment(id)}
                onToggleEnabled={(id, enabled) => handleToggleSegmentEnabled(id, enabled)}
                busySegmentIds={pendingSegmentIds}
                batchMode={segmentBatchMode}
                selectedSegmentIds={selectedSegmentIds}
                onToggleSelect={toggleSegmentSelect}
              />
            )}
          </div>
          <div
            data-testid="segment-pagination"
            className="flex items-center justify-between border-t border-border px-5 py-3 text-sm text-muted-foreground"
          >
            <span>
              {t("knowledge.detail.paginationShowing", {
                start: segmentTotal === 0 ? 0 : segmentOffset + 1,
                end: Math.min(segmentOffset + segments.length, segmentTotal),
                total: segmentTotal,
              })}
            </span>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={segmentOffset === 0 || segmentsQuery.isFetching}
                onClick={() => setSegmentOffset(Math.max(0, segmentOffset - segmentLimit))}
              >
                <ChevronLeft className="mr-1 h-4 w-4" />
                {t("knowledge.detail.previousPage")}
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={segmentOffset + segmentLimit >= segmentTotal || segmentsQuery.isFetching}
                onClick={() => setSegmentOffset(segmentOffset + segmentLimit)}
              >
                {t("knowledge.detail.nextPage")}
                <ChevronRight className="ml-1 h-4 w-4" />
              </Button>
            </div>
          </div>
        </Card>
      )}

      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t("knowledge.detail.editSegment")}</DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-1.5">
              <Label className="text-sm font-medium">
                {t("knowledge.segment.segmentTextLabel")}
              </Label>
              <Textarea value={editText} onChange={(e) => setEditText(e.target.value)} rows={10} />
            </div>
            <div className="space-y-1.5">
              <Label className="text-sm font-medium">
                {t("knowledge.segment.segmentAnswer")}
              </Label>
              <Textarea
                value={editAnswer}
                onChange={(e) => setEditAnswer(e.target.value)}
                rows={4}
                placeholder={t("knowledge.segment.segmentAnswerPlaceholder")}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-sm font-medium">
                {t("knowledge.segment.segmentKeywords")}
              </Label>
              <Input
                value={editKeywords}
                onChange={(e) => setEditKeywords(e.target.value)}
                placeholder={t("knowledge.segment.segmentKeywordsPlaceholder")}
              />
              <p className="text-xs text-muted-foreground">
                {t("knowledge.segment.segmentKeywordsHint")}
              </p>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditOpen(false)}>
              {t("knowledge.detail.cancel")}
            </Button>
            <Button
              onClick={saveEdit}
              disabled={editSaving || !editText.trim()}
              className="bg-primary hover:bg-primary/90"
            >
              {editSaving ? t("knowledge.detail.saving") : t("knowledge.detail.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <DocumentMetadataDialog
        open={metadataOpen}
        onOpenChange={setMetadataOpen}
        documents={metadataDocuments}
        registry={metadataRegistry}
        saving={metadataSaving}
        onSave={saveMetadata}
      />

      {/* 批量重建索引确认对话框 */}
      <Dialog
        open={batchReindexOpen}
        onOpenChange={(open) => {
          setBatchReindexOpen(open);
          if (!open) {
            setBatchReindexSkipped(null);
            setBatchReindexAll(false);
          }
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Zap className="h-5 w-5 text-primary" />
              {t("knowledge.detail.batchReindexTitle")}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              {batchReindexAll
                ? t("knowledge.detail.batchReindexAllText", { count: totalDocuments })
                : t("knowledge.detail.batchReindexText", { count: selectedDocIds.size })}
            </p>
            <div className="bg-muted/50 rounded-lg p-3 text-xs text-muted-foreground space-y-1">
              <p>• {t("knowledge.detail.batchReindexHint1")}</p>
              <p>• {t("knowledge.detail.batchReindexHint2")}</p>
              <p>• {t("knowledge.detail.batchReindexHint3")}</p>
            </div>
            {/* 全部被跳过（409）时展示跳过清单，供用户取消选择后重试 */}
            {batchReindexSkipped !== null && (
              <div
                data-testid="batch-reindex-skipped"
                className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 space-y-2"
              >
                <p className="text-xs font-medium text-amber-700 dark:text-amber-400">
                  {t("knowledge.detail.batchReindexSkippedLabel", { count: batchReindexSkipped.length })}
                </p>
                <div className="max-h-32 overflow-auto">
                  {batchReindexSkipped.map((docId) => {
                    const doc = docs.find((d) => d.document_id === docId);
                    return (
                      <div key={docId} className="px-1 py-0.5 text-xs text-amber-700/90 dark:text-amber-400/90 truncate">
                        {doc?.title ?? docId}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            {/* 选中的文档列表预览 */}
            {!batchReindexAll && <div className="max-h-32 overflow-auto border border-border rounded-lg">
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
            </div>}
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
                t("knowledge.detail.confirmReindex", {
                  count: batchReindexAll ? totalDocuments : selectedDocIds.size,
                })
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

      {/* 归档确认对话框（原因可选，≤255 字符——后端列宽，依赖 D2） */}
      <Dialog open={archiveOpen} onOpenChange={setArchiveOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <Archive className="h-5 w-5 text-muted-foreground" />
              {t("knowledge.detail.archiveDocTitle")}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3">
            <p className="text-sm text-muted-foreground">
              {t("knowledge.detail.archiveDocDesc", {
                title: docs.find((d) => d.document_id === archiveDocId)?.title ?? "",
              })}
            </p>
            <div className="space-y-1.5">
              <Label className="text-sm font-medium">
                {t("knowledge.detail.archiveReasonLabel")}
              </Label>
              <Textarea
                data-testid="archive-reason-input"
                value={archiveReason}
                onChange={(e) => setArchiveReason(e.target.value)}
                rows={3}
                maxLength={DOCUMENT_ARCHIVE_REASON_LIMIT}
                placeholder={t("knowledge.detail.archiveReasonPlaceholder")}
              />
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>{t("knowledge.detail.archiveReasonHint")}</span>
                <span data-testid="archive-reason-count">
                  {archiveReason.length}/{DOCUMENT_ARCHIVE_REASON_LIMIT}
                </span>
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setArchiveOpen(false)} disabled={archiveSaving}>
              {t("knowledge.detail.cancel")}
            </Button>
            <Button
              data-testid="archive-confirm"
              onClick={confirmArchive}
              disabled={archiveSaving}
              className="bg-primary hover:bg-primary/90"
            >
              {archiveSaving ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  {t("knowledge.detail.processing")}
                </>
              ) : (
                t("knowledge.detail.archiveConfirm")
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 解除归档确认 */}
      <AlertDialog
        open={unarchiveDocId !== null}
        onOpenChange={(open) => {
          if (!open) setUnarchiveDocId(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2">
              <ArchiveRestore className="h-5 w-5 text-muted-foreground" />
              {t("knowledge.detail.unarchiveDocTitle")}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {t("knowledge.detail.unarchiveDocDesc", {
                title: docs.find((d) => d.document_id === unarchiveDocId)?.title ?? "",
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("knowledge.detail.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              data-testid="unarchive-confirm"
              onClick={() => confirmUnarchive()}
            >
              {t("knowledge.detail.unarchiveConfirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
