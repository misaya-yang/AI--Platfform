/**
 * Binding Sync Config Dialog
 *
 * Dialog for configuring sync mode at the binding level.
 * Allows users to switch between manual and polling (auto) sync modes.
 */

import { useState, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2, Hand, RefreshCw, AlertCircle } from "lucide-react";

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

import { updateBinding } from "@/api/confluence";
import type { ConfluenceBinding } from "@/types/confluence";
import { getErrorMessage } from "@/lib/utils";

interface BindingSyncConfigDialogProps {
  binding: ConfluenceBinding;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onUpdated?: () => void;
}

export function BindingSyncConfigDialog({
  binding,
  open,
  onOpenChange,
  onUpdated,
}: BindingSyncConfigDialogProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  // Local state for form
  const [syncMode, setSyncMode] = useState<"manual" | "polling">(
    binding.sync_mode || "manual"
  );
  const [pollingInterval, setPollingInterval] = useState<number>(
    binding.polling_interval_minutes || 60
  );
  const [error, setError] = useState<string | null>(null);

  // Reset form when binding changes or dialog opens
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSyncMode(binding.sync_mode || "manual");
    setPollingInterval(binding.polling_interval_minutes || 60);
    setError(null);
  }, [binding, open]);

  // Update mutation
  const updateMutation = useMutation({
    mutationFn: async () => {
      return await updateBinding(binding.binding_id, {
        sync_mode: syncMode,
        polling_interval_minutes: syncMode === "polling" ? pollingInterval : undefined,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-bindings"] });
      setError(null);
      onOpenChange(false);
      onUpdated?.();
    },
    onError: (err) => {
      console.error("Failed to update binding sync config:", err);
      setError(getErrorMessage(err));
    },
  });

  const handleSave = () => {
    updateMutation.mutate();
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[425px]">
        <DialogHeader>
          <DialogTitle>{t("confluence.syncConfig.title")}</DialogTitle>
          <DialogDescription>
            {t("confluence.syncConfig.description", {
              spaceName: binding.space_name || binding.space_key,
            })}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-6 py-4">
          {/* Sync Mode Selection */}
          <div className="space-y-3">
            <Label>{t("confluence.syncConfig.mode")}</Label>
            <div className="flex gap-2">
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
              {syncMode === "manual"
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
                  <SelectItem value="60">
                    {t("confluence.syncConfig.intervals.hourly")}
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
