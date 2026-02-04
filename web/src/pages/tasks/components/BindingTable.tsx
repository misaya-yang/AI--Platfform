/**
 * BindingTable - Confluence binding table component
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  CheckCircle2,
  Clock,
  AlertCircle,
  Loader2,
  FolderSync,
} from "lucide-react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { useToast } from "@/hooks/use-toast";
import type { ConfluenceBinding } from "@/types/confluence";
import { useTriggerSync, useUpdateBinding } from "../hooks/useConfluenceSync";
import { PageManageDialog } from "./PageManageDialog";

interface BindingTableProps {
  bindings: ConfluenceBinding[];
  isLoading: boolean;
}

type TFunction = (key: string, options?: Record<string, unknown>) => string;

function formatRelativeTime(dateStr: string | null, t: TFunction): string {
  if (!dateStr) return "-";
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);

  if (diffMins < 0) {
    // Future time
    const futureMins = Math.abs(diffMins);
    if (futureMins < 60) return t("common.time.minutesLater", { count: futureMins });
    const futureHours = Math.floor(futureMins / 60);
    if (futureHours < 24) return t("common.time.hoursLater", { count: futureHours });
    return t("common.time.daysLater", { count: Math.floor(futureHours / 24) });
  }

  if (diffMins < 1) return t("common.time.justNow");
  if (diffMins < 60) return t("common.time.minutesAgo", { count: diffMins });
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return t("common.time.hoursAgo", { count: diffHours });
  const diffDays = Math.floor(diffHours / 24);
  return t("common.time.daysAgo", { count: diffDays });
}

const statusConfig = {
  completed: {
    icon: <CheckCircle2 className="h-4 w-4" />,
    color: "text-emerald-500",
    bgColor: "bg-emerald-500/10 text-emerald-600 border-emerald-500/20",
    labelKey: "tasks.confluence.bindingStatus.completed",
  },
  syncing: {
    icon: <Loader2 className="h-4 w-4 animate-spin" />,
    color: "text-blue-500",
    bgColor: "bg-blue-500/10 text-blue-600 border-blue-500/20",
    labelKey: "tasks.confluence.bindingStatus.syncing",
  },
  pending: {
    icon: <Clock className="h-4 w-4" />,
    color: "text-amber-500",
    bgColor: "bg-amber-500/10 text-amber-600 border-amber-500/20",
    labelKey: "tasks.confluence.bindingStatus.pending",
  },
  error: {
    icon: <AlertCircle className="h-4 w-4" />,
    color: "text-red-500",
    bgColor: "bg-red-500/10 text-red-600 border-red-500/20",
    labelKey: "tasks.confluence.bindingStatus.error",
  },
};

export function BindingTable({ bindings, isLoading }: BindingTableProps) {
  const { t } = useTranslation();
  const { toast } = useToast();
  const [editingBinding, setEditingBinding] = useState<ConfluenceBinding | null>(
    null
  );
  const [managingBinding, setManagingBinding] = useState<ConfluenceBinding | null>(
    null
  );
  const [syncMode, setSyncMode] = useState<"manual" | "polling">("manual");
  const [interval, setInterval] = useState("60");

  const triggerMutation = useTriggerSync();
  const updateMutation = useUpdateBinding();

  const handleSync = (bindingId: string) => {
    triggerMutation.mutate(
      { bindingId },
      {
        onSuccess: () => {
          toast({
            title: t("tasks.confluence.toast.syncTriggered"),
          });
        },
        onError: (error) => {
          toast({
            title: t("tasks.confluence.toast.syncFailed"),
            description: error instanceof Error ? error.message : String(error),
            variant: "destructive",
          });
        },
      }
    );
  };

  const handleEdit = (binding: ConfluenceBinding) => {
    setEditingBinding(binding);
    setSyncMode(binding.sync_mode || "manual");
    setInterval(String(binding.polling_interval_minutes || 60));
  };

  const handleSaveConfig = async () => {
    if (!editingBinding) return;

    try {
      await updateMutation.mutateAsync({
        bindingId: editingBinding.binding_id,
        data: {
          sync_mode: syncMode,
          polling_interval_minutes: parseInt(interval, 10),
        },
      });

      toast({
        title: t("tasks.confluence.toast.configUpdated"),
      });
      setEditingBinding(null);
    } catch (error) {
      toast({
        title: t("tasks.confluence.toast.configUpdateFailed"),
        description: error instanceof Error ? error.message : String(error),
        variant: "destructive",
      });
    }
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (bindings.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <FolderSync className="h-12 w-12 text-muted-foreground/40 mb-4" />
        <p className="text-sm font-medium text-muted-foreground">
          {t("tasks.confluence.noBindings")}
        </p>
        <p className="text-xs text-muted-foreground/70 mt-1">
          {t("tasks.confluence.noBindingsDesc")}
        </p>
      </div>
    );
  }

  return (
    <>
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("tasks.confluence.table.space")}</TableHead>
              <TableHead>{t("tasks.confluence.table.syncMode")}</TableHead>
              <TableHead>{t("tasks.confluence.table.interval")}</TableHead>
              <TableHead>{t("tasks.confluence.table.status")}</TableHead>
              <TableHead>{t("tasks.confluence.table.lastSync")}</TableHead>
              <TableHead>{t("tasks.confluence.table.nextSync")}</TableHead>
              <TableHead className="text-center w-40">
                {t("tasks.confluence.table.actions")}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {bindings.map((binding) => {
              const status =
                statusConfig[binding.status] || statusConfig.pending;
              const isSyncing =
                binding.status === "syncing" ||
                triggerMutation.isPending;

              return (
                <TableRow key={binding.binding_id}>
                  <TableCell>
                    <div className="flex flex-col">
                      <span className="font-medium">
                        {binding.space_name || binding.space_key}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {binding.synced_page_count}/{binding.total_page_count}{" "}
                        {t("tasks.confluence.pages")}
                      </span>
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant="outline" className="text-xs">
                      {binding.sync_mode === "polling"
                        ? t("tasks.confluence.auto")
                        : t("tasks.confluence.manual")}
                    </Badge>
                  </TableCell>
                  <TableCell>
                    {binding.sync_mode === "polling"
                      ? `${binding.polling_interval_minutes}${t("tasks.confluence.minutes")}`
                      : "-"}
                  </TableCell>
                  <TableCell>
                    <Badge className={cn("gap-1", status.bgColor)}>
                      {status.icon}
                      {t(status.labelKey)}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm">
                    {formatRelativeTime(binding.last_sync_at, t)}
                  </TableCell>
                  <TableCell className="text-sm">
                    {binding.sync_mode === "polling" && binding.next_sync_at
                      ? formatRelativeTime(binding.next_sync_at, t)
                      : "-"}
                  </TableCell>
                  <TableCell className="text-center">
                    <div className="flex items-center justify-center gap-3 text-sm">
                      <button
                        className="text-primary hover:text-primary/80 font-medium transition-colors"
                        onClick={() => setManagingBinding(binding)}
                      >
                        {t("common.details")}
                      </button>

                      {isSyncing ? (
                        <span className="flex items-center gap-1 text-muted-foreground cursor-not-allowed">
                          <Loader2 className="h-3 w-3 animate-spin" />
                          {t("tasks.confluence.bindingStatus.syncing")}
                        </span>
                      ) : (
                        <button
                          className="text-primary hover:text-primary/80 font-medium transition-colors"
                          onClick={() => handleSync(binding.binding_id)}
                        >
                          {t("common.refresh")}
                        </button>
                      )}

                      <button
                        className="text-primary hover:text-primary/80 font-medium transition-colors"
                        onClick={() => handleEdit(binding)}
                      >
                        {t("common.configure")}
                      </button>
                    </div>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </div>

      {/* Page Manage Dialog */}
      <PageManageDialog
        binding={managingBinding}
        open={!!managingBinding}
        onOpenChange={(open) => !open && setManagingBinding(null)}
      />

      {/* Edit Dialog */}
      <Dialog
        open={!!editingBinding}
        onOpenChange={(open) => !open && setEditingBinding(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t("tasks.confluence.editSyncConfig")}</DialogTitle>
            <DialogDescription>
              {editingBinding?.space_name || editingBinding?.space_key}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4 py-4">
            <div className="space-y-2">
              <Label>{t("tasks.confluence.syncMode")}</Label>
              <Select
                value={syncMode}
                onValueChange={(v) => setSyncMode(v as "manual" | "polling")}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="manual">
                    {t("tasks.confluence.manual")}
                  </SelectItem>
                  <SelectItem value="polling">
                    {t("tasks.confluence.auto")}
                  </SelectItem>
                </SelectContent>
              </Select>
            </div>
            {syncMode === "polling" && (
              <div className="space-y-2">
                <Label>{t("tasks.confluence.pollingInterval")}</Label>
                <div className="flex items-center gap-2">
                  <Input
                    type="number"
                    min="5"
                    max="1440"
                    value={interval}
                    onChange={(e) => setInterval(e.target.value)}
                    className="w-24"
                  />
                  <span className="text-sm text-muted-foreground">
                    {t("tasks.confluence.minutes")}
                  </span>
                </div>
                <p className="text-xs text-muted-foreground">
                  {t("tasks.confluence.intervalHint")}
                </p>
              </div>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditingBinding(null)}>
              {t("common.cancel")}
            </Button>
            <Button
              onClick={handleSaveConfig}
              disabled={updateMutation.isPending}
            >
              {updateMutation.isPending && (
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
              )}
              {t("common.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
