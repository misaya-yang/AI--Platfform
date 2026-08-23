/**
 * Tasks Page - Unified Task Manager
 *
 * Tab-based layout for managing:
 * 1. Scheduled tasks
 * 2. Task ID query
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Inbox, Search, ShieldCheck } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TaskInboxTab, TaskQueryTab } from "./components";

type TabValue = "inbox" | "query";

export function TasksPage() {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<TabValue>("inbox");

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

      <div className="flex items-start gap-3 rounded-xl border border-border/70 bg-card/60 p-4 text-sm text-muted-foreground">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" aria-hidden="true" />
        <p>{t("tasks.scopeNotice")}</p>
      </div>

      {/* Tabs */}
      <Tabs
        value={activeTab}
        onValueChange={(v) => setActiveTab(v as TabValue)}
        className="space-y-4"
      >
        <TabsList className="grid w-full max-w-md grid-cols-2">
          <TabsTrigger value="inbox" className="gap-2" aria-label={t("tasks.tabs.inbox")}>
            <Inbox className="h-4 w-4" />
            <span className="hidden sm:inline">{t("tasks.tabs.inbox")}</span>
          </TabsTrigger>
          <TabsTrigger value="query" className="gap-2" aria-label={t("tasks.tabs.query")}>
            <Search className="h-4 w-4" />
            <span className="hidden sm:inline">{t("tasks.tabs.query")}</span>
          </TabsTrigger>
        </TabsList>

        <TabsContent value="inbox" className="mt-4">
          <TaskInboxTab />
        </TabsContent>

        <TabsContent value="query" className="mt-4">
          <TaskQueryTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}

export default TasksPage;
