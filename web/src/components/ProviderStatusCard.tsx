import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useProvidersHealth } from "@/hooks/useServices";
import { useTranslation } from "react-i18next";
import {
  Cloud,
  Brain,
  Zap,
  Globe,
  Sparkles,
  CheckCircle2,
  XCircle,
} from "lucide-react";

// 供应商图标映射
const providerIcons: Record<string, React.ReactNode> = {
  openai: <Brain className="h-5 w-5" />,
  anthropic: <Sparkles className="h-5 w-5" />,
  deepseek: <Zap className="h-5 w-5" />,
  dashscope: <Cloud className="h-5 w-5" />,
  google: <Globe className="h-5 w-5" />,
};

export function ProviderStatusCard() {
  const { t } = useTranslation();
  const { data: providers, isLoading } = useProvidersHealth();

  if (isLoading) {
    return (
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base font-medium">
            {t("dashboard.providerStatus", "模型供应商状态")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-sm text-muted-foreground">
            {t("common.loading", "加载中...")}
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!providers || Object.keys(providers).length === 0) {
    return null;
  }

  const providerList = Object.entries(providers);
  const configuredCount = providerList.filter(([, p]) => p.configured).length;

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="text-base font-medium">
            {t("dashboard.providerStatus", "模型供应商状态")}
          </CardTitle>
          <Badge variant="outline" className="text-xs">
            {configuredCount}/{providerList.length} {t("dashboard.configured", "已配置")}
          </Badge>
        </div>
      </CardHeader>
      <CardContent>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5">
          {providerList.map(([key, provider]) => (
            <div
              key={key}
              className={`flex items-center gap-3 rounded-lg border p-3 ${
                provider.configured
                  ? "border-green-200 bg-green-50 dark:border-green-900 dark:bg-green-950"
                  : "border-muted bg-muted/30"
              }`}
            >
              <div
                className={`${
                  provider.configured
                    ? "text-green-600 dark:text-green-400"
                    : "text-muted-foreground"
                }`}
              >
                {providerIcons[key] || <Cloud className="h-5 w-5" />}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-sm truncate">
                    {provider.name}
                  </span>
                  {provider.configured ? (
                    <CheckCircle2 className="h-4 w-4 text-green-600 dark:text-green-400 shrink-0" />
                  ) : (
                    <XCircle className="h-4 w-4 text-muted-foreground shrink-0" />
                  )}
                </div>
                {provider.configured && provider.model_count > 0 && (
                  <div className="text-xs text-muted-foreground">
                    {provider.model_count} {t("dashboard.models", "个模型")}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}
