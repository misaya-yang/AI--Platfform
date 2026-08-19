/**
 * Tasks Page - Unified Task Manager
 *
 * Tab-based layout for managing:
 * 1. Scheduled tasks
 * 2. Task ID query
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Calendar, Search, Cpu, CheckCircle2, Layers, Clock } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TaskQueryTab, ScheduledTasksTab } from "./components";

type TabValue = "scheduled" | "query";

export function TasksPage() {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<TabValue>("scheduled");

  return (
    <div className="space-y-4">
      {/* Page Header */}
      <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            {t("tasks.title")}
          </h1>
          <p className="text-sm text-muted-foreground mt-0.5">
            {t("tasks.description")}
          </p>
        </div>
      </div>

      {/* Operational Signal Ribbon */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 p-3 rounded-xl border border-border/70 bg-card/60">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-500 shrink-0">
            <CheckCircle2 className="h-4.5 w-4.5" />
          </div>
          <div className="min-w-0">
            <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">{t("tasks.ribbon.dispatcher")}</div>
            <div className="text-sm font-bold text-foreground flex items-center gap-1.5">
              <span>Celery/Redis</span>
              <span className="text-[10px] font-semibold text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 px-1 py-0.2 rounded-xs">{t("tasks.ribbon.statusNormal")}</span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary shrink-0">
            <Layers className="h-4.5 w-4.5" />
          </div>
          <div className="min-w-0">
            <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">{t("tasks.ribbon.queue")}</div>
            <div className="text-sm font-bold tabular-nums text-foreground">
              0 <span className="text-xs font-normal text-muted-foreground">{t("tasks.ribbon.pendingTasks")}</span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-indigo-500/10 text-indigo-500 shrink-0">
            <Clock className="h-4.5 w-4.5" />
          </div>
          <div className="min-w-0">
            <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">{t("tasks.ribbon.cron")}</div>
            <div className="text-sm font-bold text-foreground">
              Crontab <span className="text-xs font-normal text-muted-foreground">{t("tasks.ribbon.cronReady")}</span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-amber-500/10 text-amber-500 shrink-0">
            <Cpu className="h-4.5 w-4.5" />
          </div>
          <div className="min-w-0">
            <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">{t("tasks.ribbon.concurrency")}</div>
            <div className="text-sm font-bold tabular-nums text-foreground">
              4 <span className="text-xs font-normal text-muted-foreground">{t("tasks.ribbon.workerThreads")}</span>
            </div>
          </div>
        </div>
      </div>

      {/* Tabs */}
      <Tabs
        value={activeTab}
        onValueChange={(v) => setActiveTab(v as TabValue)}
        className="space-y-4"
      >
        <TabsList className="grid w-full max-w-md grid-cols-2">
          <TabsTrigger value="scheduled" className="gap-2">
            <Calendar className="h-4 w-4" />
            <span className="hidden sm:inline">
              {t("tasks.tabs.scheduled")}
            </span>
          </TabsTrigger>
          <TabsTrigger value="query" className="gap-2">
            <Search className="h-4 w-4" />
            <span className="hidden sm:inline">{t("tasks.tabs.query")}</span>
          </TabsTrigger>
        </TabsList>

        <TabsContent value="scheduled" className="mt-4">
          <ScheduledTasksTab />
        </TabsContent>

        <TabsContent value="query" className="mt-4">
          <TaskQueryTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}

export default TasksPage;
