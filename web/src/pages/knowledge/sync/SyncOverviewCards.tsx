/**
 * Sync Overview Cards Component
 *
 * Displays summary statistics for sync sources.
 */

import { useMemo } from "react";
import { Cloud, FileText, Clock, RefreshCcw } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ConfluenceBinding } from "@/types/confluence";

interface SyncOverviewCardsProps {
  bindings: ConfluenceBinding[];
}

export function SyncOverviewCards({ bindings }: SyncOverviewCardsProps) {
  const { t, i18n } = useTranslation();

  const stats = useMemo(() => {
    const totalSources = bindings.length;
    const totalPages = bindings.reduce((sum, b) => sum + b.total_page_count, 0);
    const syncedPages = bindings.reduce((sum, b) => sum + b.synced_page_count, 0);
    const activeSyncs = bindings.filter((b) => b.status === "syncing").length;

    // Find most recent sync time
    const syncTimes = bindings
      .map((b) => b.last_sync_at)
      .filter(Boolean)
      .map((dateStr) => new Date(dateStr!).getTime());
    const lastSyncAt = syncTimes.length > 0 ? new Date(Math.max(...syncTimes)) : null;

    return { totalSources, totalPages, syncedPages, activeSyncs, lastSyncAt };
  }, [bindings]);

  const formatRelativeTime = (date: Date) => {
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffMins < 1) return t("knowledge.sync.justNow");
    if (diffMins < 60) return t("knowledge.sync.minutesAgo", { count: diffMins });
    if (diffHours < 24) return t("knowledge.sync.hoursAgo", { count: diffHours });
    if (diffDays < 7) return t("knowledge.sync.daysAgo", { count: diffDays });
    return date.toLocaleDateString(i18n.language === "zh-CN" ? "zh-CN" : "en-US", { month: "short", day: "numeric" });
  };

  const cards = [
    {
      key: "sources",
      icon: Cloud,
      label: t("knowledge.sync.syncSources"),
      value: stats.totalSources,
      color: "from-blue-500/10 to-cyan-500/10 border-blue-500/20",
      iconColor: "text-blue-500",
    },
    {
      key: "pages",
      icon: FileText,
      label: t("knowledge.sync.syncedPagesCount"),
      value: `${stats.syncedPages}/${stats.totalPages}`,
      color: "from-emerald-500/10 to-teal-500/10 border-emerald-500/20",
      iconColor: "text-emerald-500",
    },
    {
      key: "lastSync",
      icon: Clock,
      label: t("knowledge.sync.lastSync"),
      value: stats.lastSyncAt ? formatRelativeTime(stats.lastSyncAt) : t("knowledge.sync.never"),
      color: "from-amber-500/10 to-orange-500/10 border-amber-500/20",
      iconColor: "text-amber-500",
    },
    {
      key: "active",
      icon: RefreshCcw,
      label: t("knowledge.sync.inProgress"),
      value: stats.activeSyncs,
      color: stats.activeSyncs > 0
        ? "from-purple-500/10 to-pink-500/10 border-purple-500/20"
        : "from-slate-500/10 to-gray-500/10 border-slate-500/20",
      iconColor: stats.activeSyncs > 0 ? "text-purple-500" : "text-slate-400",
      animate: stats.activeSyncs > 0,
    },
  ];

  return (
    <div className="grid grid-cols-4 gap-4">
      {cards.map((card) => (
        <div
          key={card.key}
          className={`relative overflow-hidden rounded-xl bg-gradient-to-br ${card.color} border p-4`}
        >
          <div className="flex items-start justify-between">
            <div>
              <p className="text-sm text-muted-foreground">{card.label}</p>
              <p className="text-2xl font-semibold mt-1">{card.value}</p>
            </div>
            <div className={`${card.iconColor}`}>
              <card.icon className={`h-5 w-5 ${card.animate ? "animate-spin" : ""}`} />
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
