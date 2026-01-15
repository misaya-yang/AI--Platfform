/**
 * Sources Tab Component
 *
 * Main tab for managing data sources in dataset detail page.
 * Shows three source options: File Upload, URL Import, and Confluence Sync.
 */

import { Upload, Link, Cloud, FileText, ArrowRight } from "lucide-react";
import { useSearchParams } from "react-router-dom";

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
          返回数据源
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
      title: "文件上传",
      description: "上传本地文件到知识库，支持 PDF、Word、TXT、Markdown 等格式",
      icon: Upload,
      colorClass: {
        gradient: "from-emerald-500/10 to-teal-500/10",
        border: "border-emerald-500/20",
        iconBg: "bg-gradient-to-br from-emerald-500/10 to-teal-500/10 border-emerald-500/20",
        iconColor: "text-emerald-500",
        buttonHover: "hover:bg-emerald-500/10 hover:text-emerald-600 hover:border-emerald-500/30",
      },
      stat: {
        label: "已上传文件",
        value: documentStats.uploaded,
      },
      action: {
        label: "上传文件",
        onClick: onUploadClick,
      },
    },
    {
      key: "url",
      title: "网页导入",
      description: "从 URL 抓取网页内容导入知识库，支持单页和站点爬取",
      icon: Link,
      colorClass: {
        gradient: "from-violet-500/10 to-purple-500/10",
        border: "border-violet-500/20",
        iconBg: "bg-gradient-to-br from-violet-500/10 to-purple-500/10 border-violet-500/20",
        iconColor: "text-violet-500",
        buttonHover: "hover:bg-violet-500/10 hover:text-violet-600 hover:border-violet-500/30",
      },
      stat: {
        label: "已导入网页",
        value: documentStats.fromUrl,
      },
      action: {
        label: "导入网页",
        onClick: onUrlClick,
      },
    },
    {
      key: "confluence",
      title: "Confluence 同步",
      description: "连接 Confluence 空间，自动同步页面内容并保持更新",
      icon: Cloud,
      colorClass: {
        gradient: "from-blue-500/10 to-cyan-500/10",
        border: "border-blue-500/20",
        iconBg: "bg-gradient-to-br from-blue-500/10 to-cyan-500/10 border-blue-500/20",
        iconColor: "text-blue-500",
        buttonHover: "hover:bg-blue-500/10 hover:text-blue-600 hover:border-blue-500/30",
      },
      stat: {
        label: "已同步页面",
        value: documentStats.fromConfluence,
      },
      action: {
        label: "管理同步",
        onClick: () => setSearchParams({ source: "confluence" }),
      },
    },
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold">数据源管理</h3>
          <p className="text-sm text-muted-foreground mt-1">
            选择一种方式添加数据到知识库
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
        共 {documentStats.total} 个文档来源
      </div>
    </div>
  );
}
