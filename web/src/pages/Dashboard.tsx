import { useTranslation } from "react-i18next";
import { useServices, useHealth } from "@/hooks/useServices";
import { ServiceCard } from "@/components/ServiceCard";
import { ServiceCostAnalysis } from "@/components/ServiceCostAnalysis";
import { UserServiceUsageAnalytics } from "@/components/UserServiceUsageAnalytics";
import { SecurityEventCharts } from "@/components/SecurityEventCharts";

export function DashboardPage() {
  const { t } = useTranslation();
  const servicesQuery = useServices();
  const healthQuery = useHealth();
  const services = servicesQuery.data || [];
  const health = healthQuery.data || {};

  return (
    <div className="space-y-6">
      {/* 服务列表 */}
      <div>
        <div className="text-xl font-semibold mb-3">{t("dashboard.title")}</div>
        {servicesQuery.isLoading ? (
          <div className="text-sm text-muted-foreground">{t("dashboard.loadingServices")}</div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
            {services.map((s) => (
              <ServiceCard
                key={s.service_id}
                service={s}
                health={health[s.service_id]}
              />
            ))}
          </div>
        )}
      </div>

      {/* 服务成本分析 */}
      <ServiceCostAnalysis />

      {/* 按用户/服务历史统计 */}
      <UserServiceUsageAnalytics />

      {/* 鉴权失败/限流触发 */}
      <SecurityEventCharts />
    </div>
  );
}
