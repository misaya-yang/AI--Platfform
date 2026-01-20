/**
 * SchedulerStatus - Scheduler status bar component
 */

import { useTranslation } from "react-i18next";
import { Activity, Pause, Play, RefreshCw, Loader2 } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { ConfluenceSchedulerStatus } from "@/types/confluence";

interface SchedulerStatusProps {
  status: ConfluenceSchedulerStatus | undefined;
  isLoading: boolean;
  onRefresh: () => void;
}

export function SchedulerStatus({
  status,
  isLoading,
  onRefresh,
}: SchedulerStatusProps) {
  const { t } = useTranslation();

  if (!status) {
    return (
      <div className="flex items-center justify-between p-3 rounded-lg bg-muted/50 border">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span>{t("tasks.confluence.scheduler.loading")}</span>
        </div>
      </div>
    );
  }

  const isRunning = status.is_running;
  const activeCount = status.active_sync_count;
  const totalTasks = status.task_count;

  return (
    <div className="flex items-center justify-between p-3 rounded-lg bg-muted/50 border">
      <div className="flex items-center gap-4">
        {/* Status indicator */}
        <div className="flex items-center gap-2">
          <div
            className={cn(
              "w-2 h-2 rounded-full",
              isRunning ? "bg-emerald-500 animate-pulse" : "bg-gray-400"
            )}
          />
          <span className="text-sm font-medium">
            {t("tasks.confluence.scheduler.title")}:
          </span>
          <Badge
            variant={isRunning ? "default" : "secondary"}
            className={cn(
              "text-xs",
              isRunning && "bg-emerald-500/10 text-emerald-600 border-emerald-500/20"
            )}
          >
            {isRunning ? (
              <>
                <Play className="h-3 w-3 mr-1" />
                {t("tasks.confluence.scheduler.running")}
              </>
            ) : (
              <>
                <Pause className="h-3 w-3 mr-1" />
                {t("tasks.confluence.scheduler.stopped")}
              </>
            )}
          </Badge>
        </div>

        {/* Stats */}
        {isRunning && (
          <>
            <div className="h-4 w-px bg-border" />
            <div className="flex items-center gap-1 text-sm">
              <Activity className="h-4 w-4 text-blue-500" />
              <span className="text-muted-foreground">
                {t("tasks.confluence.scheduler.active")}:
              </span>
              <span className="font-medium">
                {activeCount}/{status.max_concurrent}
              </span>
            </div>
            <div className="h-4 w-px bg-border" />
            <div className="flex items-center gap-1 text-sm">
              <span className="text-muted-foreground">
                {t("tasks.confluence.scheduler.tasks")}:
              </span>
              <span className="font-medium">{totalTasks}</span>
            </div>
          </>
        )}
      </div>

      {/* Actions */}
      <Button
        variant="ghost"
        size="sm"
        onClick={onRefresh}
        disabled={isLoading}
        className="h-7"
      >
        <RefreshCw
          className={cn("h-3.5 w-3.5", isLoading && "animate-spin")}
        />
      </Button>
    </div>
  );
}
