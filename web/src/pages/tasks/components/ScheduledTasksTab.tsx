/**
 * ScheduledTasksTab - Placeholder for future scheduled tasks
 */

import { useTranslation } from "react-i18next";
import { Calendar } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";

export function ScheduledTasksTab() {
  const { t } = useTranslation();

  return (
    <Card className="border border-border/70 bg-card">
      <CardContent className="flex flex-col items-center justify-center px-6 py-12 text-center sm:py-16">
        <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-muted/60 border border-border/60">
          <Calendar className="h-5 w-5 text-muted-foreground" />
        </div>
        <h3 className="mt-4 text-base font-semibold text-foreground">
          {t("tasks.scheduled.comingSoon")}
        </h3>
        <p className="mt-1 max-w-md text-sm text-muted-foreground">
          {t("tasks.scheduled.comingSoonDesc")}
        </p>
      </CardContent>
    </Card>
  );
}
