import { useTranslation } from "react-i18next";
import { useServices, useHealth } from "@/hooks/useServices";
import { ServiceCard } from "@/components/ServiceCard";
import { MetricsChart } from "@/components/MetricsChart";

export function DashboardPage() {
  const { t } = useTranslation();
  const servicesQuery = useServices();
  const healthQuery = useHealth();
  const services = servicesQuery.data || [];
  const health = healthQuery.data || {};

  return (
    <div className="space-y-4">
      <div className="text-xl font-semibold">{t("dashboard.title")}</div>
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
      <MetricsChart />
    </div>
  );
}
