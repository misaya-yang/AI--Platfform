import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  Inbox,
  Loader2,
  RefreshCw,
  XCircle,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { useTasks } from "@/hooks/useTasks";
import type { Task } from "@/types/gateway";

const FILTERS = ["all", "pending", "processing", "completed", "failed", "cancelled"] as const;
type TaskFilter = (typeof FILTERS)[number];

function TaskStatusBadge({ status }: { status: string }) {
  const { t } = useTranslation();
  const icon = status === "completed"
    ? <CheckCircle2 className="h-3.5 w-3.5" />
    : status === "failed"
      ? <AlertCircle className="h-3.5 w-3.5" />
      : status === "cancelled"
        ? <XCircle className="h-3.5 w-3.5" />
        : status === "processing"
          ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
          : <Clock3 className="h-3.5 w-3.5" />;
  return (
    <Badge variant="outline" className="gap-1.5">
      {icon}
      {t(`tasks.status.${status}`, status)}
    </Badge>
  );
}

function TaskRow({ task }: { task: Task }) {
  const { t, i18n } = useTranslation();
  const rawProgress = Number(task.progress ?? 0);
  const progress = Math.min(100, Math.max(0, rawProgress <= 1 ? rawProgress * 100 : rawProgress));
  return (
    <article className="grid gap-3 rounded-xl border border-border/70 bg-card p-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <strong className="truncate text-sm">{task.service_id}</strong>
          <TaskStatusBadge status={task.status} />
        </div>
        <p className="mt-1 truncate font-mono text-xs text-muted-foreground">{task.task_id}</p>
        <p className="mt-1 text-xs text-muted-foreground">
          {t("tasks.createdAt")} · {new Date(task.created_at).toLocaleString(i18n.language)}
        </p>
      </div>
      <div className="text-left sm:text-right">
        <span className="text-xs text-muted-foreground">{t("tasks.progress")}</span>
        <div className="mt-1 font-mono text-sm tabular-nums">{Math.round(progress)}%</div>
      </div>
    </article>
  );
}

export function TaskInboxTab() {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<TaskFilter>("all");
  const status = filter === "all" ? undefined : filter;
  const query = useTasks(status);
  const tasks = useMemo(() => query.data ?? [], [query.data]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap gap-2" role="group" aria-label={t("tasks.inbox.filterLabel")}>
          {FILTERS.map((value) => (
            <Button
              key={value}
              size="sm"
              variant={filter === value ? "default" : "outline"}
              aria-pressed={filter === value}
              onClick={() => setFilter(value)}
            >
              {t(`tasks.inbox.filters.${value}`)}
            </Button>
          ))}
        </div>
        <Button size="sm" variant="ghost" onClick={() => void query.refetch()} disabled={query.isFetching}>
          <RefreshCw className={`mr-2 h-3.5 w-3.5 ${query.isFetching ? "animate-spin" : ""}`} />
          {t("common.refresh")}
        </Button>
      </div>

      {query.isLoading ? (
        <Card><CardContent className="flex items-center justify-center gap-2 py-14 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />{t("tasks.states.loading")}
        </CardContent></Card>
      ) : query.isError ? (
        <Card className="border-destructive/30"><CardContent className="flex flex-col items-center py-12 text-center">
          <AlertCircle className="h-7 w-7 text-destructive" />
          <p className="mt-3 text-sm font-medium">{t("tasks.inbox.loadFailed")}</p>
          <Button className="mt-3" size="sm" variant="outline" onClick={() => void query.refetch()}>{t("tasks.states.retry")}</Button>
        </CardContent></Card>
      ) : tasks.length === 0 ? (
        <Card className="border-dashed"><CardContent className="flex flex-col items-center py-14 text-center">
          <Inbox className="h-8 w-8 text-muted-foreground/60" />
          <p className="mt-3 text-sm font-medium">{t("tasks.inbox.empty")}</p>
          <p className="mt-1 max-w-md text-xs text-muted-foreground">{t("tasks.inbox.emptyDescription")}</p>
        </CardContent></Card>
      ) : (
        <div className="space-y-3">{tasks.map((task) => <TaskRow key={task.task_id} task={task} />)}</div>
      )}
    </div>
  );
}
