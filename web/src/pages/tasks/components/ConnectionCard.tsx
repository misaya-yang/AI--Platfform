/**
 * ConnectionCard - Confluence connection card component
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Globe,
  CheckCircle2,
  XCircle,
  AlertCircle,
  RefreshCw,
  Settings,
  Loader2,
  ChevronRight,
} from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { ConfluenceConnection } from "@/types/confluence";
import { useTestConnection } from "../hooks/useConfluenceSync";

interface ConnectionCardProps {
  connection: ConfluenceConnection;
  bindingCount: number;
  lastSyncAt: string | null;
  isSelected: boolean;
  onSelect: () => void;
  onEdit?: () => void;
}

type TFunction = (key: string, options?: Record<string, unknown>) => string;

function formatRelativeTime(dateStr: string | null, t: TFunction): string {
  if (!dateStr) return "-";
  const date = new Date(dateStr);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);

  if (diffMins < 1) return t("common.time.justNow");
  if (diffMins < 60) return t("common.time.minutesAgo", { count: diffMins });
  const diffHours = Math.floor(diffMins / 60);
  if (diffHours < 24) return t("common.time.hoursAgo", { count: diffHours });
  const diffDays = Math.floor(diffHours / 24);
  return t("common.time.daysAgo", { count: diffDays });
}

export function ConnectionCard({
  connection,
  bindingCount,
  lastSyncAt,
  isSelected,
  onSelect,
  onEdit,
}: ConnectionCardProps) {
  const { t } = useTranslation();
  const [isTesting, setIsTesting] = useState(false);
  const testMutation = useTestConnection();

  const handleTest = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setIsTesting(true);
    try {
      await testMutation.mutateAsync(connection.connection_id);
    } finally {
      setIsTesting(false);
    }
  };

  const statusConfig = {
    active: {
      icon: <CheckCircle2 className="h-4 w-4" />,
      color: "text-emerald-500",
      bgColor: "bg-emerald-500",
      label: t("tasks.confluence.status.active"),
    },
    disabled: {
      icon: <XCircle className="h-4 w-4" />,
      color: "text-gray-400",
      bgColor: "bg-gray-400",
      label: t("tasks.confluence.status.disabled"),
    },
    error: {
      icon: <AlertCircle className="h-4 w-4" />,
      color: "text-red-500",
      bgColor: "bg-red-500",
      label: t("tasks.confluence.status.error"),
    },
  };

  const status = statusConfig[connection.status] || statusConfig.disabled;

  return (
    <Card
      className={cn(
        "cursor-pointer transition-all duration-200 hover:shadow-md",
        isSelected
          ? "ring-2 ring-primary border-primary bg-primary/5"
          : "hover:border-primary/50"
      )}
      onClick={onSelect}
    >
      <CardContent className="p-4">
        {/* Header */}
        <div className="flex items-start justify-between mb-3">
          <div className="flex items-center gap-2 min-w-0">
            <div className={cn("w-2 h-2 rounded-full", status.bgColor)} />
            <Globe className="h-4 w-4 text-muted-foreground flex-shrink-0" />
            <span className="font-medium text-sm truncate">
              {connection.domain}
            </span>
          </div>
          <ChevronRight
            className={cn(
              "h-4 w-4 text-muted-foreground transition-transform",
              isSelected && "rotate-90"
            )}
          />
        </div>

        {/* Info */}
        <div className="space-y-2 text-xs text-muted-foreground">
          <div className="flex items-center justify-between">
            <span>{t("tasks.confluence.syncMode")}:</span>
            <Badge variant="outline" className="text-xs">
              {connection.sync_mode === "polling"
                ? t("tasks.confluence.auto")
                : t("tasks.confluence.manual")}
              {connection.sync_mode === "polling" &&
                ` (${connection.polling_interval_minutes}${t("tasks.confluence.minutes")})`}
            </Badge>
          </div>
          <div className="flex items-center justify-between">
            <span>{t("tasks.confluence.bindings")}:</span>
            <span className="font-medium text-foreground">
              {bindingCount} {t("tasks.confluence.spaces")}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span>{t("tasks.confluence.lastSync")}:</span>
            <span className="font-medium text-foreground">
              {formatRelativeTime(lastSyncAt, t)}
            </span>
          </div>
        </div>

        {/* Actions */}
        <div className="flex gap-2 mt-3 pt-3 border-t">
          <Button
            variant="outline"
            size="sm"
            className="flex-1 h-7 text-xs"
            onClick={handleTest}
            disabled={isTesting}
          >
            {isTesting ? (
              <Loader2 className="h-3 w-3 animate-spin mr-1" />
            ) : (
              <RefreshCw className="h-3 w-3 mr-1" />
            )}
            {t("tasks.confluence.test")}
          </Button>
          {onEdit && (
            <Button
              variant="ghost"
              size="sm"
              className="h-7 px-2"
              onClick={(e) => {
                e.stopPropagation();
                onEdit();
              }}
            >
              <Settings className="h-3 w-3" />
            </Button>
          )}
        </div>

        {/* Error message */}
        {connection.last_error && (
          <div className="mt-2 p-2 rounded bg-red-50 dark:bg-red-900/20 text-xs text-red-600 dark:text-red-400">
            {connection.last_error}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
