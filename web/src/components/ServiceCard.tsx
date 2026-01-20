import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { HealthStatus, ServiceDefinition, ServiceType } from "@/types/gateway";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ServiceConfigDialog } from "@/components/ServiceConfigDialog";
import { cn } from "@/lib/utils";
import {
  MessageSquare,
  Sparkles,
  Cog,
  BarChart3,
  Tag,
  GitBranch,
  Wrench,
  Package,
  Settings,
  Bot,
  Globe,
} from "lucide-react";

// Simple icon components for service types
function ServiceIcon({ serviceType }: { serviceType: ServiceType }) {
  const iconProps = { className: "h-6 w-6" };

  switch (serviceType) {
    case "assistant":
      return <Bot {...iconProps} />;
    case "conversational":
      return <MessageSquare {...iconProps} />;
    case "generative":
      return <Sparkles {...iconProps} />;
    case "processing":
      return <Cog {...iconProps} />;
    case "embedding":
      return <BarChart3 {...iconProps} />;
    case "classification":
      return <Tag {...iconProps} />;
    case "langgraph":
      return <GitBranch {...iconProps} />;
    case "proxy":
      return <Globe {...iconProps} />;
    case "custom":
      return <Wrench {...iconProps} />;
    default:
      return <Package {...iconProps} />;
  }
}

// 服务类型中文标签
const serviceTypeLabels: Record<string, string> = {
  assistant: "AI 助手",
  langgraph: "LangGraph",
  proxy: "代理",
  conversational: "对话",
  generative: "生成",
  processing: "处理",
  embedding: "嵌入",
  classification: "分类",
  custom: "自定义",
};

export function ServiceCard({
  service,
  health,
  onSelect,
  selected,
}: {
  service: ServiceDefinition;
  health?: HealthStatus;
  selected?: boolean;
  onSelect?: () => void;
}) {
  const { t } = useTranslation();
  const [configOpen, setConfigOpen] = useState(false);
  const isHealthy = health?.status === "healthy";
  const isVirtual = service.metadata?.is_virtual === true;
  const serviceTypeLabel = serviceTypeLabels[service.service_type] || service.service_type;

  return (
    <>
      <Card
        className={cn(
          "relative overflow-hidden cursor-pointer group transition-all duration-300 border bg-card hover:shadow-md hover:border-primary/50",
          selected && "ring-2 ring-primary border-primary shadow-sm"
        )}
        onClick={onSelect}
      >
        <CardContent className="p-4 relative">
          <div className="flex items-center gap-4">
            {/* Icon */}
            <div className="flex-shrink-0 p-2.5 rounded-lg bg-primary/10 text-primary group-hover:bg-primary group-hover:text-primary-foreground transition-colors duration-300 border border-primary/20">
              <ServiceIcon serviceType={service.service_type} />
            </div>

            {/* Content */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 mb-1">
                <h3 className="font-semibold text-foreground truncate">
                  {service.name}
                </h3>
                {/* Status Indicator */}
                <div className="flex-shrink-0" title={isHealthy ? t("services.status.active", "Active") : t("services.status.inactive", "Inactive")}>
                  <span className={cn(
                    "inline-flex rounded-full h-2 w-2",
                    isHealthy ? "bg-green-500" : "bg-red-500"
                  )}></span>
                </div>
              </div>
              <p className="font-mono text-xs text-muted-foreground truncate mb-1.5">
                {service.service_id}
              </p>
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge variant="secondary" className="bg-slate-100 text-slate-700 dark:bg-slate-800 dark:text-slate-300 text-[10px] font-semibold border border-slate-200 dark:border-slate-700 px-1.5 py-0">
                  {serviceTypeLabel}
                </Badge>
                {isVirtual && (
                  <Badge variant="outline" className="text-[10px] font-semibold border-blue-200 text-blue-700 bg-blue-50 dark:bg-blue-900/30 dark:text-blue-300 dark:border-blue-800 px-1.5 py-0">
                    {t("services.badge.core", "Core")}
                  </Badge>
                )}
                {service.supported_modes?.slice(0, 2).map((m) => (
                  <Badge key={m} variant="outline" className="text-[10px] text-muted-foreground font-medium border-border bg-background px-1.5 py-0">
                    {m}
                  </Badge>
                ))}
              </div>
            </div>

            {/* Actions */}
            <div className="flex-shrink-0 flex items-center gap-1">
              {!isVirtual && (
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-muted-foreground hover:text-foreground hover:bg-muted transition-all duration-200"
                  onClick={(e) => {
                    e.stopPropagation();
                    setConfigOpen(true);
                  }}
                  title="Config"
                >
                  <Settings className="h-4 w-4" />
                </Button>
              )}
            </div>
          </div>

          {/* Description - optional, shown if exists */}
          {service.description && (
            <p className="text-xs text-muted-foreground mt-3 pt-3 border-t border-border/50 line-clamp-1">
              {service.description}
            </p>
          )}
        </CardContent>
      </Card>

      {!isVirtual && (
        <ServiceConfigDialog
          serviceId={service.service_id}
          serviceName={service.name}
          open={configOpen}
          onOpenChange={setConfigOpen}
        />
      )}
    </>
  );
}
