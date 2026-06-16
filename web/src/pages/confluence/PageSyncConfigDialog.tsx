/**
 * Page Sync Config Dialog
 *
 * Dialog for configuring sync mode at the page level.
 * Allows users to set custom sync intervals for individual pages,
 * or inherit from the binding-level configuration.
 */

import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Hand, RefreshCw, AlertCircle, ArrowDown } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";

import { updatePageSyncConfig } from "@/api/confluence";
import type { ConfluencePageRecord } from "@/types/confluence";
import { getErrorMessage } from "@/lib/utils";

interface PageSyncConfigDialogProps {
  page: ConfluencePageRecord;
  bindingId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSuccess?: () => void;
}

export function PageSyncConfigDialog({
  page,
  bindingId,
  open,
  onOpenChange,
  onSuccess,
}: PageSyncConfigDialogProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  // Local state for form
  // null = inherit from binding
  const [syncMode, setSyncMode] = useState<"manual" | "polling" | null>(
    page.sync_mode
  );
  const [pollingInterval, setPollingInterval] = useState<number>(
    page.polling_interval_minutes || 60
  );
  const [syncEnabled, setSyncEnabled] = useState<boolean>(page.sync_enabled);
  const [syncPriority, setSyncPriority] = useState<number>(page.sync_priority || 0);
  const [error, setError] = useState<string | null>(null);

  // Reset form when page changes or dialog opens
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSyncMode(page.sync_mode);
    setPollingInterval(page.polling_interval_minutes || 60);
    setSyncEnabled(page.sync_enabled);
    setSyncPriority(page.sync_priority || 0);
    setError(null);
  }, [page, open]);

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: async () => {
      return await updatePageSyncConfig(page.id, {
        sync_mode: syncMode,
        polling_interval_minutes: syncMode === "polling" ? pollingInterval : undefined,
        sync_enabled: syncEnabled,
        sync_priority: syncPriority,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-pages", bindingId] });
      setError(null);
      onOpenChange(false);
      onSuccess?.();
    },
    onError: (err) => {
      console.error("Failed to update page sync config:", err);
      setError(getErrorMessage(err));
    },
  });

  const handleSave = () => {
    updateMutation.mutate();
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[480px]">
        <DialogHeader>
          <DialogTitle>{t("confluence.pageSyncConfig.title")}</DialogTitle>
          <DialogDescription className="line-clamp-2">
            {t("confluence.pageSyncConfig.description", {
              pageTitle: page.title,
            })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-6 py-4">
          {/* Sync Enabled Toggle */}
          <div className="flex items-center justify-between">
            <div className="space-y-0.5">
              <Label>{t("confluence.pageSyncConfig.enabled")}</Label>
              <p className="text-xs text-muted-foreground">
                {t("confluence.pageSyncConfig.enabledDesc")}
              </p>
            </div>
            <Switch
              checked={syncEnabled}
              onCheckedChange={setSyncEnabled}
            />
          </div>

          {/* Sync Mode Selection */}
          <div className="space-y-3">
            <Label>{t("confluence.pageSyncConfig.mode")}</Label>
            <div className="flex gap-2">
              <Button
                type="button"
                variant={syncMode === null ? "default" : "outline-solid"}
                className="flex-1"
                onClick={() => setSyncMode(null)}
              >
                <ArrowDown className="h-4 w-4 mr-2" />
                {t("confluence.pageSyncConfig.inherit")}
              </Button>
              <Button
                type="button"
                variant={syncMode === "manual" ? "default" : "outline-solid"}
                className="flex-1"
                onClick={() => setSyncMode("manual")}
              >
                <Hand className="h-4 w-4 mr-2" />
                {t("confluence.syncConfig.manual")}
              </Button>
              <Button
                type="button"
                variant={syncMode === "polling" ? "default" : "outline-solid"}
                className="flex-1"
                onClick={() => setSyncMode("polling")}
              >
                <RefreshCw className="h-4 w-4 mr-2" />
                {t("confluence.syncConfig.polling")}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {syncMode === null
                ? t("confluence.pageSyncConfig.inheritDesc")
                : syncMode === "manual"
                  ? t("confluence.syncConfig.manualDesc")
                  : t("confluence.syncConfig.pollingDesc")}
            </p>
          </div>

          {/* Polling Interval (only shown for polling mode) */}
          {syncMode === "polling" && (
            <div className="space-y-3">
              <Label>{t("confluence.syncConfig.interval")}</Label>
              <Select
                value={String(pollingInterval)}
                onValueChange={(v) => setPollingInterval(Number(v))}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="30">
                    {t("confluence.syncConfig.intervals.30min")}
                  </SelectItem>
                  <SelectItem value="60">
                    {t("confluence.syncConfig.intervals.hourly")}
                  </SelectItem>
                  <SelectItem value="180">
                    {t("confluence.syncConfig.intervals.3hours")}
                  </SelectItem>
                  <SelectItem value="360">
                    {t("confluence.syncConfig.intervals.6hours")}
                  </SelectItem>
                  <SelectItem value="720">
                    {t("confluence.syncConfig.intervals.12hours")}
                  </SelectItem>
                  <SelectItem value="1440">
                    {t("confluence.syncConfig.intervals.daily")}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
          )}

          {/* Sync Priority */}
          <div className="space-y-3">
            <Label>{t("confluence.pageSyncConfig.priority")}</Label>
            <Select
              value={String(syncPriority)}
              onValueChange={(v) => setSyncPriority(Number(v))}
            >
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="0">
                  {t("confluence.pageSyncConfig.priorities.normal")}
                </SelectItem>
                <SelectItem value="50">
                  {t("confluence.pageSyncConfig.priorities.high")}
                </SelectItem>
                <SelectItem value="100">
                  {t("confluence.pageSyncConfig.priorities.critical")}
                </SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              {t("confluence.pageSyncConfig.priorityDesc")}
            </p>
          </div>

          {/* Error display */}
          {error && (
            <div className="p-3 rounded-lg bg-rose-50 dark:bg-rose-950/30 border border-rose-200 dark:border-rose-900 flex items-center gap-2">
              <AlertCircle className="h-4 w-4 text-rose-600 shrink-0" />
              <span className="text-sm text-rose-700 dark:text-rose-400">{error}</span>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            {t("common.cancel")}
          </Button>
          <Button onClick={handleSave} disabled={updateMutation.isPending}>
            {updateMutation.isPending && (
              <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            )}
            {t("common.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
