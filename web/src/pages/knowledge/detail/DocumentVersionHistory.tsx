/**
 * Document Version History Component
 *
 * Shows version history for a document with compare and restore functionality.
 */

import { useState, useEffect } from "react";
import {
  History,
  GitCompare,
  RotateCcw,
  Clock,
  FileText,
  Cloud,
  Loader2,
  Plus,
  Minus,
  ArrowRight,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
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
import { toast } from "@/hooks/use-toast";
import {
  listDocumentVersions,
  compareDocumentVersions,
  restoreDocumentVersion,
  type DocumentVersion,
  type VersionCompareResponse,
} from "@/api/knowledge";

interface DocumentVersionHistoryProps {
  datasetId: string;
  documentId: string;
  documentTitle: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRestored?: () => void;
}

export function DocumentVersionHistory({
  datasetId,
  documentId,
  documentTitle,
  open,
  onOpenChange,
  onRestored,
}: DocumentVersionHistoryProps) {
  const [versions, setVersions] = useState<DocumentVersion[]>([]);
  const [currentVersion, setCurrentVersion] = useState<number>(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);

  // Compare mode
  const [compareMode, setCompareMode] = useState(false);
  const [selectedForCompare, setSelectedForCompare] = useState<number[]>([]);
  const [compareResult, setCompareResult] = useState<VersionCompareResponse | null>(null);
  const [comparing, setComparing] = useState(false);

  // Restore
  const [restoreVersion, setRestoreVersion] = useState<number | null>(null);
  const [restoring, setRestoring] = useState(false);

  useEffect(() => {
    if (open && datasetId && documentId) {
      loadVersions();
    }
  }, [open, datasetId, documentId]);

  async function loadVersions() {
    setLoading(true);
    try {
      const result = await listDocumentVersions(datasetId, documentId);
      setVersions(result.versions);
      setCurrentVersion(result.current_version);
      setTotal(result.total);
    } catch (err) {
      toast({
        title: "加载版本历史失败",
        description: String(err),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  }

  function handleSelectForCompare(versionNumber: number) {
    if (selectedForCompare.includes(versionNumber)) {
      setSelectedForCompare(selectedForCompare.filter((v) => v !== versionNumber));
    } else if (selectedForCompare.length < 2) {
      setSelectedForCompare([...selectedForCompare, versionNumber]);
    } else {
      // Replace the first selection
      setSelectedForCompare([selectedForCompare[1], versionNumber]);
    }
  }

  async function handleCompare() {
    if (selectedForCompare.length !== 2) return;

    const [v1, v2] = selectedForCompare.sort((a, b) => a - b);
    setComparing(true);
    try {
      const result = await compareDocumentVersions(datasetId, documentId, v1, v2);
      setCompareResult(result);
    } catch (err) {
      toast({
        title: "版本对比失败",
        description: String(err),
        variant: "destructive",
      });
    } finally {
      setComparing(false);
    }
  }

  async function handleRestore() {
    if (!restoreVersion) return;

    setRestoring(true);
    try {
      await restoreDocumentVersion(datasetId, documentId, restoreVersion);
      toast({
        title: "版本恢复成功",
        description: `已恢复到版本 #${restoreVersion}`,
      });
      setRestoreVersion(null);
      onOpenChange(false);
      onRestored?.();
    } catch (err) {
      toast({
        title: "版本恢复失败",
        description: String(err),
        variant: "destructive",
      });
    } finally {
      setRestoring(false);
    }
  }

  function getChangeTypeBadge(changeType: string) {
    const styles: Record<string, string> = {
      created: "bg-green-100 text-green-700 border-green-200",
      updated: "bg-blue-100 text-blue-700 border-blue-200",
      restored: "bg-amber-100 text-amber-700 border-amber-200",
      deleted: "bg-red-100 text-red-700 border-red-200",
    };
    const labels: Record<string, string> = {
      created: "创建",
      updated: "更新",
      restored: "回滚",
      deleted: "删除",
    };
    return (
      <Badge variant="outline" className={styles[changeType] || ""}>
        {labels[changeType] || changeType}
      </Badge>
    );
  }

  function formatDate(dateStr: string) {
    return new Date(dateStr).toLocaleString("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-w-3xl max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <History className="h-5 w-5 text-primary" />
              版本历史
              <span className="text-sm font-normal text-muted-foreground">
                - {documentTitle}
              </span>
            </DialogTitle>
          </DialogHeader>

          {/* Toolbar */}
          <div className="flex items-center justify-between border-b border-border pb-3">
            <div className="flex items-center gap-2">
              <Badge variant="secondary">
                共 {total} 个版本
              </Badge>
              <Badge variant="outline">
                当前版本: #{currentVersion}
              </Badge>
            </div>
            <div className="flex items-center gap-2">
              {!compareMode ? (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCompareMode(true)}
                  disabled={versions.length < 2}
                >
                  <GitCompare className="h-4 w-4 mr-1.5" />
                  版本对比
                </Button>
              ) : (
                <>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setCompareMode(false);
                      setSelectedForCompare([]);
                      setCompareResult(null);
                    }}
                  >
                    取消对比
                  </Button>
                  <Button
                    size="sm"
                    onClick={handleCompare}
                    disabled={selectedForCompare.length !== 2 || comparing}
                  >
                    {comparing ? (
                      <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                    ) : (
                      <GitCompare className="h-4 w-4 mr-1.5" />
                    )}
                    对比选中 ({selectedForCompare.length}/2)
                  </Button>
                </>
              )}
            </div>
          </div>

          {/* Content */}
          <div className="flex-1 overflow-hidden">
            {loading ? (
              <div className="flex items-center justify-center h-48">
                <Loader2 className="h-8 w-8 animate-spin text-primary" />
              </div>
            ) : compareResult ? (
              /* Compare Result View */
              <div className="h-full flex flex-col">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2 text-sm">
                    <Badge variant="outline">版本 #{compareResult.from_version}</Badge>
                    <ArrowRight className="h-4 w-4 text-muted-foreground" />
                    <Badge variant="outline">版本 #{compareResult.to_version}</Badge>
                  </div>
                  <div className="flex items-center gap-3 text-sm">
                    <span className="text-green-600 flex items-center gap-1">
                      <Plus className="h-3.5 w-3.5" />
                      {compareResult.stats.additions} 新增
                    </span>
                    <span className="text-red-600 flex items-center gap-1">
                      <Minus className="h-3.5 w-3.5" />
                      {compareResult.stats.deletions} 删除
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setCompareResult(null)}
                    >
                      <X className="h-4 w-4" />
                    </Button>
                  </div>
                </div>
                <div className="flex-1 border rounded-lg overflow-auto">
                  <div className="p-3 font-mono text-sm">
                    {compareResult.diff.map((item, idx) => (
                      <div
                        key={idx}
                        className={`px-2 py-0.5 ${
                          item.type === "insert"
                            ? "bg-green-50 text-green-800"
                            : item.type === "delete"
                            ? "bg-red-50 text-red-800"
                            : "text-muted-foreground"
                        }`}
                      >
                        <span className="inline-block w-6 text-muted-foreground/50">
                          {item.type === "insert" ? "+" : item.type === "delete" ? "-" : " "}
                        </span>
                        {item.content}
                      </div>
                    ))}
                    {compareResult.diff.length === 0 && (
                      <div className="text-center py-8 text-muted-foreground">
                        两个版本内容相同
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ) : versions.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-48 text-muted-foreground">
                <History className="h-12 w-12 mb-3 opacity-50" />
                <p>暂无版本历史</p>
                <p className="text-sm mt-1">文档更新时将自动保存版本</p>
              </div>
            ) : (
              /* Version List */
              <div className="h-[400px] pr-4 overflow-auto">
                <div className="space-y-2">
                  {versions.map((version) => (
                    <div
                      key={version.version_id}
                      className={`
                        p-4 rounded-lg border transition-colors
                        ${
                          compareMode && selectedForCompare.includes(version.version_number)
                            ? "border-primary bg-primary/5"
                            : "border-border hover:border-primary/30 hover:bg-muted/30"
                        }
                        ${version.version_number === currentVersion ? "border-primary/50" : ""}
                      `}
                    >
                      <div className="flex items-start justify-between">
                        <div className="flex items-start gap-3">
                          {compareMode && (
                            <input
                              type="checkbox"
                              checked={selectedForCompare.includes(version.version_number)}
                              onChange={() => handleSelectForCompare(version.version_number)}
                              className="mt-1 h-4 w-4 rounded border-border"
                            />
                          )}
                          <div>
                            <div className="flex items-center gap-2">
                              <span className="font-semibold">
                                版本 #{version.version_number}
                              </span>
                              {version.version_number === currentVersion && (
                                <Badge className="bg-primary/10 text-primary border-primary/20">
                                  当前版本
                                </Badge>
                              )}
                              {getChangeTypeBadge(version.change_type)}
                            </div>
                            <div className="flex items-center gap-4 mt-2 text-sm text-muted-foreground">
                              <span className="flex items-center gap-1">
                                <Clock className="h-3.5 w-3.5" />
                                {formatDate(version.created_at)}
                              </span>
                              <span className="flex items-center gap-1">
                                <FileText className="h-3.5 w-3.5" />
                                {version.word_count} 字
                              </span>
                              {version.confluence_version && (
                                <span className="flex items-center gap-1">
                                  <Cloud className="h-3.5 w-3.5" />
                                  Confluence v{version.confluence_version}
                                </span>
                              )}
                            </div>
                            {version.change_reason && (
                              <p className="text-sm text-muted-foreground mt-1.5 line-clamp-1">
                                {version.change_reason}
                              </p>
                            )}
                          </div>
                        </div>
                        {!compareMode && version.version_number !== currentVersion && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setRestoreVersion(version.version_number)}
                            className="text-amber-600 hover:text-amber-700 hover:bg-amber-50"
                          >
                            <RotateCcw className="h-4 w-4 mr-1.5" />
                            回滚
                          </Button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* Restore Confirmation Dialog */}
      <AlertDialog open={restoreVersion !== null} onOpenChange={() => setRestoreVersion(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认回滚到版本 #{restoreVersion}？</AlertDialogTitle>
            <AlertDialogDescription className="space-y-2">
              <span className="block">
                将把文档 "{documentTitle}" 恢复到版本 #{restoreVersion} 的内容。
              </span>
              <span className="block text-amber-600">
                当前内容将被保存为新版本，此操作可撤销。
              </span>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={restoring}>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleRestore}
              disabled={restoring}
              className="bg-amber-600 hover:bg-amber-700"
            >
              {restoring ? (
                <>
                  <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                  恢复中...
                </>
              ) : (
                <>
                  <RotateCcw className="h-4 w-4 mr-1.5" />
                  确认回滚
                </>
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
