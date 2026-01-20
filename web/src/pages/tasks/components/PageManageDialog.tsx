/**
 * PageManageDialog - Manage sync configuration for individual pages
 */

import { useState, useMemo, useEffect } from "react";
import { useTranslation } from "react-i18next";
import {
  FileText,
  Loader2,
  Search,
  CheckCircle2,
  Clock,
  AlertCircle,
  ToggleLeft,
  ToggleRight,
} from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Checkbox } from "@/components/ui/checkbox";
import { cn } from "@/lib/utils";
import { useToast } from "@/hooks/use-toast";
import type { ConfluenceBinding, ConfluencePageRecord } from "@/types/confluence";
import { useBindingPages, useUpdatePageSyncConfig } from "../hooks/useConfluenceSync";

interface PageManageDialogProps {
  binding: ConfluenceBinding | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const statusConfig = {
  synced: {
    icon: <CheckCircle2 className="h-3.5 w-3.5" />,
    color: "text-emerald-500",
    bgColor: "bg-emerald-500/10 text-emerald-600",
  },
  pending: {
    icon: <Clock className="h-3.5 w-3.5" />,
    color: "text-amber-500",
    bgColor: "bg-amber-500/10 text-amber-600",
  },
  needs_resync: {
    icon: <Clock className="h-3.5 w-3.5" />,
    color: "text-blue-500",
    bgColor: "bg-blue-500/10 text-blue-600",
  },
  error: {
    icon: <AlertCircle className="h-3.5 w-3.5" />,
    color: "text-red-500",
    bgColor: "bg-red-500/10 text-red-600",
  },
};

export function PageManageDialog({
  binding,
  open,
  onOpenChange,
}: PageManageDialogProps) {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [searchQuery, setSearchQuery] = useState("");
  const [selectedPages, setSelectedPages] = useState<Set<string>>(new Set());

  const { data: pagesData, isLoading } = useBindingPages(binding?.binding_id);
  const updateMutation = useUpdatePageSyncConfig();

  const pages = pagesData?.pages || [];

  // Reset state when dialog closes or binding changes
  useEffect(() => {
    if (!open) {
      setSearchQuery("");
      setSelectedPages(new Set());
    }
  }, [open]);

  // Filter pages by search query
  const filteredPages = useMemo(() => {
    if (!searchQuery.trim()) return pages;
    const query = searchQuery.toLowerCase();
    return pages.filter((page) =>
      page.title.toLowerCase().includes(query)
    );
  }, [pages, searchQuery]);

  // Stats
  const enabledCount = pages.filter((p) => p.sync_enabled).length;
  const disabledCount = pages.length - enabledCount;

  const handleToggleSync = async (page: ConfluencePageRecord) => {
    try {
      await updateMutation.mutateAsync({
        pageRecordId: page.id,
        bindingId: binding?.binding_id,
        data: { sync_enabled: !page.sync_enabled },
      });
      toast({
        title: page.sync_enabled
          ? t("tasks.confluence.toast.pageSyncDisabled")
          : t("tasks.confluence.toast.pageSyncEnabled"),
      });
    } catch (error) {
      toast({
        title: t("tasks.confluence.toast.pageSyncUpdateFailed"),
        description: error instanceof Error ? error.message : String(error),
        variant: "destructive",
      });
    }
  };

  const handleBatchToggle = async (enable: boolean) => {
    if (selectedPages.size === 0) return;

    const targetPages = pages.filter((p) => selectedPages.has(p.id));
    let successCount = 0;
    let errorCount = 0;

    for (const page of targetPages) {
      if (page.sync_enabled === enable) continue;
      try {
        await updateMutation.mutateAsync({
          pageRecordId: page.id,
          bindingId: binding?.binding_id,
          data: { sync_enabled: enable },
        });
        successCount++;
      } catch {
        errorCount++;
      }
    }

    if (successCount > 0) {
      toast({
        title: enable
          ? t("tasks.confluence.toast.batchEnabled", { count: successCount })
          : t("tasks.confluence.toast.batchDisabled", { count: successCount }),
      });
    }
    if (errorCount > 0) {
      toast({
        title: t("tasks.confluence.toast.batchPartialFailed", { count: errorCount }),
        variant: "destructive",
      });
    }
    setSelectedPages(new Set());
  };

  const handleSelectAll = () => {
    if (selectedPages.size === filteredPages.length) {
      setSelectedPages(new Set());
    } else {
      setSelectedPages(new Set(filteredPages.map((p) => p.id)));
    }
  };

  const handleSelectPage = (pageId: string, checked: boolean) => {
    const newSelected = new Set(selectedPages);
    if (checked) {
      newSelected.add(pageId);
    } else {
      newSelected.delete(pageId);
    }
    setSelectedPages(newSelected);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[80vh]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <FileText className="h-5 w-5" />
            {t("tasks.confluence.pageManage.title")}
          </DialogTitle>
        </DialogHeader>

        {/* Stats & Search */}
        <div className="flex items-center justify-between gap-4 py-2">
          <div className="flex items-center gap-3 text-sm">
            <span className="text-muted-foreground">
              {t("tasks.confluence.pageManage.total")}: <strong>{pages.length}</strong>
            </span>
            <Badge variant="outline" className="bg-emerald-500/10 text-emerald-600">
              <ToggleRight className="h-3 w-3 mr-1" />
              {enabledCount}
            </Badge>
            <Badge variant="outline" className="bg-muted text-muted-foreground">
              <ToggleLeft className="h-3 w-3 mr-1" />
              {disabledCount}
            </Badge>
          </div>
          <div className="relative w-64">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder={t("tasks.confluence.pageManage.searchPlaceholder")}
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-8 h-8"
            />
          </div>
        </div>

        {/* Batch Actions */}
        {selectedPages.size > 0 && (
          <div className="flex items-center gap-2 p-2 bg-muted/50 rounded-md">
            <span className="text-sm text-muted-foreground">
              {t("tasks.confluence.pageManage.selected", { count: selectedPages.size })}
            </span>
            <Button
              size="sm"
              variant="outline"
              className="h-7"
              onClick={() => handleBatchToggle(true)}
              disabled={updateMutation.isPending}
            >
              {t("tasks.confluence.pageManage.enableSelected")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="h-7"
              onClick={() => handleBatchToggle(false)}
              disabled={updateMutation.isPending}
            >
              {t("tasks.confluence.pageManage.disableSelected")}
            </Button>
          </div>
        )}

        {/* Page List */}
        <ScrollArea className="h-[400px] border rounded-md">
          {isLoading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
            </div>
          ) : filteredPages.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <FileText className="h-12 w-12 text-muted-foreground/40 mb-4" />
              <p className="text-sm text-muted-foreground">
                {searchQuery
                  ? t("tasks.confluence.pageManage.noSearchResults")
                  : t("tasks.confluence.pageManage.noPages")}
              </p>
            </div>
          ) : (
            <div className="divide-y">
              {/* Header */}
              <div className="flex items-center gap-3 px-4 py-2 bg-muted/30 sticky top-0">
                <Checkbox
                  checked={selectedPages.size === filteredPages.length && filteredPages.length > 0}
                  onCheckedChange={handleSelectAll}
                />
                <span className="flex-1 text-xs font-medium text-muted-foreground uppercase">
                  {t("tasks.confluence.pageManage.pageName")}
                </span>
                <span className="w-24 text-xs font-medium text-muted-foreground uppercase text-center">
                  {t("tasks.confluence.pageManage.status")}
                </span>
                <span className="w-20 text-xs font-medium text-muted-foreground uppercase text-center">
                  {t("tasks.confluence.pageManage.sync")}
                </span>
              </div>

              {/* Page Rows */}
              {filteredPages.map((page) => {
                const status = statusConfig[page.effective_status || page.status] || statusConfig.pending;
                return (
                  <div
                    key={page.id}
                    className={cn(
                      "flex items-center gap-3 px-4 py-2 hover:bg-muted/30 transition-colors",
                      !page.sync_enabled && "opacity-60"
                    )}
                  >
                    <Checkbox
                      checked={selectedPages.has(page.id)}
                      onCheckedChange={(checked) =>
                        handleSelectPage(page.id, checked as boolean)
                      }
                    />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium truncate">{page.title}</p>
                      <p className="text-xs text-muted-foreground">
                        {page.web_url ? (
                          <a
                            href={page.web_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="hover:underline"
                          >
                            {page.page_id}
                          </a>
                        ) : (
                          page.page_id
                        )}
                      </p>
                    </div>
                    <div className="w-24 flex justify-center">
                      <Badge className={cn("gap-1 text-xs", status.bgColor)}>
                        {status.icon}
                        {t(`tasks.confluence.pageStatus.${page.effective_status || page.status}`)}
                      </Badge>
                    </div>
                    <div className="w-20 flex justify-center">
                      <Button
                        variant="ghost"
                        size="sm"
                        className={cn(
                          "h-7 px-2",
                          page.sync_enabled
                            ? "text-emerald-600 hover:text-emerald-700"
                            : "text-muted-foreground hover:text-foreground"
                        )}
                        onClick={() => handleToggleSync(page)}
                        disabled={updateMutation.isPending}
                        aria-label={page.sync_enabled ? t("common.disable") : t("common.enable")}
                        title={page.sync_enabled ? t("tasks.confluence.toast.pageSyncDisabled") : t("tasks.confluence.toast.pageSyncEnabled")}
                      >
                        {page.sync_enabled ? (
                          <ToggleRight className="h-5 w-5" />
                        ) : (
                          <ToggleLeft className="h-5 w-5" />
                        )}
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </ScrollArea>

        {/* Footer hint */}
        <p className="text-xs text-muted-foreground">
          {t("tasks.confluence.pageManage.hint")}
        </p>
      </DialogContent>
    </Dialog>
  );
}
