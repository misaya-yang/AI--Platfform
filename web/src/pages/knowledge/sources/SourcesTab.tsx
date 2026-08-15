/**
 * Sources Tab Component
 *
 * Main tab for managing data sources in dataset detail page.
 * Shows two source options: File Upload and URL Import.
 */

import { Upload, Link, FileText, ArrowRight } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

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
  onUploadClick,
  onUrlClick,
  documentStats,
}: SourcesTabProps) {
  const { t } = useTranslation();

  // Source cards configuration
  const sourceCards = [
    {
      key: "file",
      title: t("knowledge.sources.fileUpload"),
      description: t("knowledge.sources.fileUploadDesc"),
      icon: Upload,
      colorClass: {
        border: "border-border/80",
        iconBg: "bg-primary/10 border-primary/15",
        iconColor: "text-primary",
        buttonHover: "hover:bg-muted/60 hover:text-foreground",
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
        border: "border-border/80",
        iconBg: "bg-primary/10 border-primary/15",
        iconColor: "text-primary",
        buttonHover: "hover:bg-muted/60 hover:text-foreground",
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
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3 lg:gap-6">
        {sourceCards.map((card) => {
          const IconComponent = card.icon;
          return (
            <Card
              key={card.key}
              variant="interactive"
              className={`relative overflow-hidden ${card.colorClass.border} p-5 sm:p-6`}
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
                className={`h-10 w-full sm:h-8 ${card.colorClass.buttonHover}`}
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
