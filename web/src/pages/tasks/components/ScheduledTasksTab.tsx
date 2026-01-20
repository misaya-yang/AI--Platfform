/**
 * ScheduledTasksTab - Placeholder for future scheduled tasks
 */

import { useTranslation } from "react-i18next";
import { Calendar, Sparkles } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";

export function ScheduledTasksTab() {
  const { t } = useTranslation();

  return (
    <Card className="border-dashed">
      <CardContent className="flex flex-col items-center justify-center py-20">
        <div className="relative">
          <div className="rounded-full bg-primary/5 p-6">
            <Calendar className="h-12 w-12 text-primary/40" />
          </div>
          <div className="absolute -top-1 -right-1 rounded-full bg-amber-500/10 p-1.5">
            <Sparkles className="h-4 w-4 text-amber-500" />
          </div>
        </div>
        <h3 className="mt-6 text-lg font-semibold">
          {t("tasks.scheduled.comingSoon")}
        </h3>
        <p className="mt-2 text-sm text-muted-foreground text-center max-w-md">
          {t("tasks.scheduled.comingSoonDesc")}
        </p>
        <div className="mt-6 flex flex-wrap gap-2 justify-center">
          <span className="px-3 py-1 rounded-full bg-muted text-xs font-medium">
            {t("tasks.scheduled.feature.dailyNews")}
          </span>
          <span className="px-3 py-1 rounded-full bg-muted text-xs font-medium">
            {t("tasks.scheduled.feature.reportGen")}
          </span>
          <span className="px-3 py-1 rounded-full bg-muted text-xs font-medium">
            {t("tasks.scheduled.feature.dataSync")}
          </span>
        </div>
      </CardContent>
    </Card>
  );
}
