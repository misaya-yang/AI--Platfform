/**
 * Sources Tab Component
 *
 * Main tab for managing data sources in dataset detail page.
 * Shows three source options: File Upload, URL Import, and Confluence Sync.
 */

import { Upload, Link, Cloud, FileText, ArrowRight } from "lucide-react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

import { SyncSourcesTab } from "@/pages/knowledge/sync/SyncSourcesTab";

interface SourcesTabProps {
  datasetId: string;
  onUploadClick: () => void;
  onUrlClick: () => void;
  documentStats: {
    total: number;
    uploaded: number;
    fromUrl: number;
    fromConfluence: number;
  };
}

export function SourcesTab({
  datasetId,
  onUploadClick,
  onUrlClick,
  documentStats,
}: SourcesTabProps) {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const showConfluence = searchParams.get("source") === "confluence";

  // If Confluence panel is active, show the full SyncSourcesTab
  if (showConfluence) {
    return (
      <div className="space-y-4">
        {/* Back Button */}
        <Button
          variant="ghost"
          size="sm"
          onClick={() => setSearchParams({})}
          className="mb-2"
        >
          <ArrowRight className="h-4 w-4 mr-1.5 rotate-180" />
          {t("knowledge.sources.backToSources")}
        </Button>

        {/* Full Confluence Sync UI */}
        <SyncSourcesTab datasetId={datasetId} />
      </div>
    );
  }

  // Source cards configuration
  const sourceCards = [
    {
      key: "file",
      title: t("knowledge.sources.fileUpload"),
      description: t("knowledge.sources.fileUploadDesc"),
      icon: Upload,
      colorClass: {
        gradient: "from-emerald-500/10 to-teal-500/10",
        border: "border-emerald-500/20",
        iconBg: "bg-gradient-to-br from-emerald-500/10 to-teal-500/10 border-emerald-500/20",
        iconColor: "text-emerald-500",
        buttonHover: "hover:bg-emerald-500/10 hover:text-emerald-600 hover:border-emerald-500/30",
      },
      stat: {
        label: t("knowledge.sources.uploadedFiles"),
        value: documentStats.uploaded,
      },
      action: {
        label: t("knowledge.sources.uploadFile"),
        onClick: onUploadClick,
      },
    },
    {
      key: "url",
      title: t("knowledge.sources.webImport"),
      description: t("knowledge.sources.webImportDesc"),
      icon: Link,
      colorClass: {
        gradient: "from-violet-500/10 to-purple-500/10",
        border: "border-violet-500/20",
        iconBg: "bg-gradient-to-br from-violet-500/10 to-purple-500/10 border-violet-500/20",
        iconColor: "text-violet-500",
        buttonHover: "hover:bg-violet-500/10 hover:text-violet-600 hover:border-violet-500/30",
      },
      stat: {
        label: t("knowledge.sources.importedPages"),
        value: documentStats.fromUrl,
      },
      action: {
        label: t("knowledge.sources.importPage"),
        onClick: onUrlClick,
      },
    },
    {
      key: "confluence",
      title: t("knowledge.sources.confluenceSync"),
      description: t("knowledge.sources.confluenceSyncDesc"),
      icon: Cloud,
      colorClass: {
        gradient: "from-blue-500/10 to-cyan-500/10",
        border: "border-blue-500/20",
        iconBg: "bg-gradient-to-br from-blue-500/10 to-cyan-500/10 border-blue-500/20",
        iconColor: "text-blue-500",
        buttonHover: "hover:bg-blue-500/10 hover:text-blue-600 hover:border-blue-500/30",
      },
      stat: {
        label: t("knowledge.sources.syncedPages"),
        value: documentStats.fromConfluence,
      },
      action: {
        label: t("knowledge.sources.manageSync"),
        onClick: () => setSearchParams({ source: "confluence" }),
      },
    },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold">{t("knowledge.sources.sourceManagement")}</h3>
          <p className="text-sm text-muted-foreground mt-1">
            {t("knowledge.sources.sourceManagementDesc")}
          </p>
        </div>
      </div>

      {/* Source Cards Grid */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {sourceCards.map((card) => {
          const IconComponent = card.icon;
          return (
            <Card
              key={card.key}
              className={`relative overflow-hidden bg-gradient-to-br ${card.colorClass.gradient} ${card.colorClass.border} p-6 transition-all duration-200 hover:shadow-md`}
            >
              {/* Icon */}
              <div
                className={`w-12 h-12 rounded-xl ${card.colorClass.iconBg} border flex items-center justify-center mb-4`}
              >
                <IconComponent className={`h-6 w-6 ${card.colorClass.iconColor}`} />
              </div>

              {/* Title & Description */}
              <h4 className="font-semibold text-base mb-2">{card.title}</h4>
              <p className="text-sm text-muted-foreground mb-4 line-clamp-2">
                {card.description}
              </p>

              {/* Stats */}
              <div className="flex items-center gap-2 mb-4 text-sm">
                <FileText className="h-4 w-4 text-muted-foreground" />
                <span className="text-muted-foreground">{card.stat.label}:</span>
                <span className="font-medium">{card.stat.value}</span>
              </div>

              {/* Action Button */}
              <Button
                variant="outline"
                size="sm"
                className={`w-full ${card.colorClass.buttonHover}`}
                onClick={card.action.onClick}
              >
                {card.action.label}
                <ArrowRight className="h-4 w-4 ml-1.5" />
              </Button>
            </Card>
          );
        })}
      </div>

      {/* Total Summary */}
      <div className="text-center text-sm text-muted-foreground pt-4 border-t">
        {t("knowledge.sources.totalDocSources", { count: documentStats.total })}
      </div>
    </div>
  );
}
