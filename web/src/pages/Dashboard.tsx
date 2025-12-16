import { useServices, useHealth } from "@/hooks/useServices";
import { ServiceCard } from "@/components/ServiceCard";
import { MetricsChart } from "@/components/MetricsChart";

export function DashboardPage() {
  const servicesQuery = useServices();
  const healthQuery = useHealth();
  const services = servicesQuery.data || [];
  const health = healthQuery.data || {};

  return (
    <div className="space-y-4">
      <div className="text-xl font-semibold">仪表盘</div>
      {servicesQuery.isLoading ? (
        <div className="text-sm text-muted-foreground">加载服务中...</div>
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
