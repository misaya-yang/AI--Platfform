/**
 * Tasks Page - Unified Task Manager
 *
 * Tab-based layout for managing:
 * 1. Scheduled tasks
 * 2. Task ID query
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Calendar, Search } from "lucide-react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TaskQueryTab, ScheduledTasksTab } from "./components";

type TabValue = "scheduled" | "query";

export function TasksPage() {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<TabValue>("scheduled");

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">
            {t("tasks.title")}
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            {t("tasks.description")}
          </p>
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
