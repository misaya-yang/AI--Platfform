/**
 * TaskQueryTab - Task ID query tab (migrated from original Tasks.tsx)
 */

import { useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import {
  Search,
  Loader2,
  AlertCircle,
  CheckCircle2,
  Clock,
  XCircle,
  RefreshCw,
  FileSearch,
  ArrowRight,
  Copy,
  Check,
} from "lucide-react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useTask, useTaskResult } from "@/hooks/useTasks";
import { copyToClipboard } from "@/lib/clipboard";

const STATUS_CONFIG: Record<
  string,
  {
    labelKey: string;
    variant: "default" | "secondary" | "destructive" | "outline";
    icon: React.ReactNode;
    color: string;
  }
> = {
  completed: {
    labelKey: "tasks.status.completed",
    variant: "default",
    icon: <CheckCircle2 className="h-3.5 w-3.5" />,
    color: "bg-emerald-500/10 text-emerald-600 border-emerald-500/20",
  },
  running: {
    labelKey: "tasks.status.running",
    variant: "secondary",
    icon: <Loader2 className="h-3.5 w-3.5 animate-spin" />,
    color: "bg-blue-500/10 text-blue-600 border-blue-500/20",
  },
  pending: {
    labelKey: "tasks.status.pending",
    variant: "outline",
    icon: <Clock className="h-3.5 w-3.5" />,
    color: "bg-amber-500/10 text-amber-600 border-amber-500/20",
  },
  failed: {
    labelKey: "tasks.status.failed",
    variant: "destructive",
    icon: <XCircle className="h-3.5 w-3.5" />,
    color: "bg-red-500/10 text-red-600 border-red-500/20",
  },
  cancelled: {
    labelKey: "tasks.status.cancelled",
    variant: "outline",
    icon: <XCircle className="h-3.5 w-3.5" />,
    color: "bg-gray-500/10 text-gray-500 border-gray-500/20",
  },
};

function StatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  const config = STATUS_CONFIG[status] || {
    labelKey: status,
    variant: "outline" as const,
    icon: <Clock className="h-3.5 w-3.5" />,
    color: "bg-gray-500/10 text-gray-500 border-gray-500/20",
  };

  return (
    <Badge className={`gap-1.5 ${config.color} border font-medium`}>
      {config.icon}
      {t(config.labelKey)}
    </Badge>
  );
}

export function TaskQueryTab() {
  const { t, i18n } = useTranslation();
  const [inputValue, setInputValue] = useState("");
  const [searchedId, setSearchedId] = useState<string | undefined>(undefined);
  const [copied, setCopied] = useState(false);

  const taskQuery = useTask(searchedId);
  const resultQuery = useTaskResult(searchedId);

  const handleSearch = useCallback(() => {
    const trimmed = inputValue.trim();
    if (!trimmed) return;
    setSearchedId(trimmed);
  }, [inputValue]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") {
        handleSearch();
      }
    },
    [handleSearch]
  );

  const handleCopyId = useCallback(() => {
    if (searchedId) {
      copyToClipboard(searchedId);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, [searchedId]);

  const hasSearched = searchedId !== undefined;
  const isLoading = taskQuery.isLoading || taskQuery.isFetching;
  const hasError = taskQuery.isError;
  const hasData = !!taskQuery.data;
  const isEmpty = hasSearched && !isLoading && !hasError && !hasData;

  return (
    <div className="space-y-6">
      {/* Search area */}
      <Card className="overflow-hidden">
        <CardContent className="p-4">
          <div className="flex gap-3">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground/60" />
              <Input
                className="pl-9 h-10 bg-background"
                placeholder={t("tasks.searchPlaceholder")}
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={handleKeyDown}
              />
            </div>
            <Button
              onClick={handleSearch}
              disabled={!inputValue.trim() || isLoading}
              className="h-10 px-5"
            >
              {isLoading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  {t("tasks.querying")}
                </>
              ) : (
                <>
                  {t("tasks.query")}
                  <ArrowRight className="ml-2 h-4 w-4" />
                </>
              )}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Loading state */}
      {isLoading && (
        <Card className="border-dashed bg-card/40">
          <CardContent className="flex flex-col items-center justify-center py-16">
            <div className="rounded-xl bg-primary/10 p-4 border border-primary/20">
              <Loader2 className="h-7 w-7 text-primary animate-spin" />
            </div>
            <p className="mt-4 text-sm font-medium text-foreground">
              {t("tasks.states.loading")}
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              Task ID: {searchedId}
            </p>
          </CardContent>
        </Card>
      )}

      {/* Error state */}
      {hasError && !isLoading && (
        <Card className="border-destructive/30 bg-destructive/5">
          <CardContent className="flex flex-col items-center justify-center py-12">
            <div className="rounded-full bg-destructive/10 p-4">
              <AlertCircle className="h-8 w-8 text-destructive" />
            </div>
            <p className="mt-4 text-sm font-medium text-destructive">
              {t("tasks.states.error")}
            </p>
            <p className="text-xs text-muted-foreground mt-1 max-w-md text-center">
              {taskQuery.error instanceof Error
                ? taskQuery.error.message
                : t("tasks.states.errorDesc")}
            </p>
            <Button
              variant="outline"
              size="sm"
              className="mt-4"
              onClick={() => taskQuery.refetch()}
            >
              <RefreshCw className="mr-2 h-3.5 w-3.5" />
              {t("tasks.states.retry")}
            </Button>
          </CardContent>
        </Card>
      )}

      {/* Empty state */}
      {isEmpty && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-16">
            <div className="rounded-full bg-muted p-4">
              <FileSearch className="h-8 w-8 text-muted-foreground/60" />
            </div>
            <p className="mt-4 text-sm font-medium text-foreground">
              {t("tasks.states.notFound")}
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              {t("tasks.states.notFoundDesc", { id: searchedId })}
            </p>
          </CardContent>
        </Card>
      )}

      {/* Task details */}
      {hasData && !isLoading && (
        <div className="space-y-4">
          {/* Task info card */}
          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-start justify-between">
                <div className="space-y-1">
                  <CardTitle className="text-base">
                    {t("tasks.taskDetail")}
                  </CardTitle>
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <span className="font-mono">{searchedId}</span>
                    <button
                      onClick={handleCopyId}
                      className="p-1 hover:bg-muted rounded transition-colors"
                      title={t("common.copy")}
                    >
                      {copied ? (
                        <Check className="h-3 w-3 text-green-500" />
                      ) : (
                        <Copy className="h-3 w-3" />
                      )}
                    </button>
                  </div>
                </div>
                <StatusBadge status={taskQuery.data.status} />
              </div>
            </CardHeader>
            <CardContent className="pt-0">
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-1">
                  <p className="text-xs text-muted-foreground">
                    {t("tasks.service")}
                  </p>
                  <p className="text-sm font-medium">
                    {taskQuery.data.service_id}
                  </p>
                </div>
                <div className="space-y-1">
                  <p className="text-xs text-muted-foreground">
                    {t("tasks.createdAt")}
                  </p>
                  <p className="text-sm font-medium">
                    {new Date(taskQuery.data.created_at).toLocaleString(
                      i18n.language
                    )}
                  </p>
                </div>
              </div>

              {/* Error info */}
              {taskQuery.data.error && (
                <div className="mt-4 p-3 rounded-lg bg-destructive/10 border border-destructive/20">
                  <div className="flex items-start gap-2">
                    <AlertCircle className="h-4 w-4 text-destructive mt-0.5 shrink-0" />
                    <div>
                      <p className="text-xs font-medium text-destructive">
                        {t("tasks.errorInfo")}
                      </p>
                      <p className="text-xs text-destructive/80 mt-1">
                        {taskQuery.data.error}
                      </p>
                    </div>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>

          {/* Task result */}
          {resultQuery.data && (
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">
                  {t("tasks.taskResult")}
                </CardTitle>
              </CardHeader>
              <CardContent className="pt-0">
                <pre className="rounded-lg bg-muted/50 border p-4 text-xs font-mono whitespace-pre-wrap overflow-auto max-h-96">
                  {JSON.stringify(resultQuery.data, null, 2)}
                </pre>
              </CardContent>
            </Card>
          )}
        </div>
      )}

      {/* Initial state hint */}
      {!hasSearched && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-16">
            <div className="rounded-full bg-primary/5 p-4">
              <Search className="h-8 w-8 text-primary/40" />
            </div>
            <p className="mt-4 text-sm font-medium text-foreground">
              {t("tasks.states.initial")}
            </p>
            <p className="text-xs text-muted-foreground mt-1">
              {t("tasks.states.initialDesc")}
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
