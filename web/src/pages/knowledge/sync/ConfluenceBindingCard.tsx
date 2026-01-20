/**
 * Confluence Binding Card Component
 *
 * Displays a single Confluence binding with its status and actions.
 */

import { useState } from "react";
import {
  Cloud,
  FileText,
  RefreshCcw,
  Settings,
  Trash2,
  CheckCircle,
  AlertCircle,
  Loader2,
  Clock,
  MoreHorizontal,
  Eye,
  Hand,
} from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
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

import { triggerSync, deleteBinding } from "@/api/confluence";
import type { ConfluenceBinding, ConfluenceConnection } from "@/types/confluence";
import { BindingSyncConfigDialog } from "@/pages/confluence/BindingSyncConfigDialog";

interface ConfluenceBindingCardProps {
  binding: ConfluenceBinding;
  connection?: ConfluenceConnection;
  datasetId: string;
  onViewPages: () => void;
  onDeleted?: () => void;
}

export function ConfluenceBindingCard({
  binding,
  connection,
  datasetId,
  onViewPages,
  onDeleted,
}: ConfluenceBindingCardProps) {
  const queryClient = useQueryClient();
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [showConfigDialog, setShowConfigDialog] = useState(false);
  const [deleteDocuments, setDeleteDocuments] = useState(false);

  // Status configurations
  const statusConfig = {
    pending: {
      color: "bg-amber-500/10 text-amber-600 border-amber-200",
      icon: <Clock className="h-3.5 w-3.5" />,
      label: "待同步",
    },
    syncing: {
      color: "bg-blue-500/10 text-blue-600 border-blue-200",
      icon: <Loader2 className="h-3.5 w-3.5 animate-spin" />,
      label: "同步中",
    },
    completed: {
      color: "bg-emerald-500/10 text-emerald-600 border-emerald-200",
      icon: <CheckCircle className="h-3.5 w-3.5" />,
      label: "已完成",
    },
    error: {
      color: "bg-rose-500/10 text-rose-600 border-rose-200",
      icon: <AlertCircle className="h-3.5 w-3.5" />,
      label: "错误",
    },
  };

  const syncModeConfig = {
    manual: {
      icon: <Hand className="h-3.5 w-3.5" />,
      label: "手动同步",
    },
    polling: {
      icon: <Clock className="h-3.5 w-3.5" />,
      label: `每 ${binding.polling_interval_minutes} 分钟`,
    },
  };

  const status = statusConfig[binding.status] || statusConfig.pending;
  const syncMode = syncModeConfig[binding.sync_mode] || syncModeConfig.manual;

  // Sync mutation
  const syncMutation = useMutation({
    mutationFn: () => triggerSync(binding.binding_id, { force: false }),
    onSuccess: () => {
      toast.success("同步已触发", "正在后台同步 Confluence 页面");
      queryClient.invalidateQueries({ queryKey: ["kb-confluence-bindings", datasetId] });
    },
    onError: (error) => {
      toast.error("同步失败", error instanceof Error ? error.message : String(error));
    },
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: () => deleteBinding(binding.binding_id, deleteDocuments),
    onSuccess: () => {
      toast.success("绑定已删除", deleteDocuments ? "相关文档已一并删除" : "文档已保留");
      queryClient.invalidateQueries({ queryKey: ["kb-confluence-bindings", datasetId] });
      onDeleted?.();
    },
    onError: (error) => {
      toast.error("删除失败", error instanceof Error ? error.message : String(error));
    },
  });

  // Display title - use root_page_titles if available, otherwise space_name
  const displayTitle = binding.root_page_titles?.length > 0
    ? binding.root_page_titles.join(", ")
    : binding.space_name || binding.space_key;

  const truncatedTitle = displayTitle.length > 50
    ? displayTitle.slice(0, 47) + "..."
    : displayTitle;

  return (
    <>
      <div className="group relative border rounded-lg p-4 hover:border-primary/30 hover:shadow-sm transition-all bg-card">
        <div className="flex items-start justify-between gap-4">
          {/* Left: Info */}
          <div className="flex-1 min-w-0">
            {/* Connection name */}
            {connection && (
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground mb-1">
                <Cloud className="h-3 w-3" />
                <span>{connection.name}</span>
                <span className="text-muted-foreground/50">•</span>
                <span>{connection.domain}</span>
              </div>
            )}

            {/* Space/Page title */}
            <div className="flex items-center gap-2 mb-2">
              <h4 className="font-medium text-foreground truncate" title={displayTitle}>
                {truncatedTitle}
              </h4>
              <Badge variant="outline" className="text-xs shrink-0">
                {binding.space_key}
              </Badge>
            </div>

            {/* Stats row */}
            <div className="flex items-center gap-4 text-sm text-muted-foreground">
              {/* Status badge */}
              <Badge
                variant="outline"
                className={`${status.color} flex items-center gap-1.5`}
              >
                {status.icon}
                {status.label}
              </Badge>

              {/* Page count */}
              <div className="flex items-center gap-1.5">
                <FileText className="h-3.5 w-3.5" />
                <span>{binding.synced_page_count}/{binding.total_page_count} 页</span>
              </div>

              {/* Sync mode */}
              <div className="flex items-center gap-1.5">
                {syncMode.icon}
                <span>{syncMode.label}</span>
              </div>

              {/* Last sync */}
              {binding.last_sync_at && (
                <div className="flex items-center gap-1.5 text-xs">
                  <Clock className="h-3 w-3" />
                  <span>
                    {new Date(binding.last_sync_at).toLocaleString("zh-CN", {
                      month: "short",
                      day: "numeric",
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                </div>
              )}
            </div>

            {/* Error message */}
            {binding.status === "error" && binding.last_error && (
              <div className="mt-2 text-xs text-rose-600 bg-rose-50 dark:bg-rose-950/20 rounded px-2 py-1">
                {binding.last_error}
              </div>
            )}
          </div>

          {/* Right: Actions */}
          <div className="flex items-center gap-2 shrink-0">
            <Button
              variant="ghost"
              size="sm"
              onClick={onViewPages}
              className="text-muted-foreground hover:text-foreground"
            >
              <Eye className="h-4 w-4 mr-1.5" />
              查看页面
            </Button>

            <Button
              variant="ghost"
              size="sm"
              onClick={() => syncMutation.mutate()}
              disabled={syncMutation.isPending || binding.status === "syncing"}
              className="text-muted-foreground hover:text-foreground"
            >
              {syncMutation.isPending || binding.status === "syncing" ? (
                <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
              ) : (
                <RefreshCcw className="h-4 w-4 mr-1.5" />
              )}
              同步
            </Button>

            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon" className="h-8 w-8">
                  <MoreHorizontal className="h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem onClick={() => setShowConfigDialog(true)}>
                  <Settings className="h-4 w-4 mr-2" />
                  同步配置
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  className="text-destructive focus:text-destructive"
                  onClick={() => setShowDeleteDialog(true)}
                >
                  <Trash2 className="h-4 w-4 mr-2" />
                  解除绑定
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </div>

      {/* Delete Confirmation Dialog */}
      <AlertDialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>确认解除绑定</AlertDialogTitle>
            <AlertDialogDescription>
              确定要解除与 "{truncatedTitle}" 的同步绑定吗？
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="py-4">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                checked={deleteDocuments}
                onChange={(e) => setDeleteDocuments(e.target.checked)}
                className="rounded border-gray-300"
              />
              同时删除已同步的文档
            </label>
            <p className="text-xs text-muted-foreground mt-1 ml-6">
              如果取消勾选，已同步的文档将保留在知识库中
            </p>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => deleteMutation.mutate()}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              disabled={deleteMutation.isPending}
            >
              {deleteMutation.isPending ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : null}
              确认解除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Sync Config Dialog */}
      <BindingSyncConfigDialog
        binding={binding}
        open={showConfigDialog}
        onOpenChange={setShowConfigDialog}
        onUpdated={() => {
          queryClient.invalidateQueries({ queryKey: ["kb-confluence-bindings", datasetId] });
        }}
      />
    </>
  );
}
