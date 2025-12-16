import { useQueryClient } from "@tanstack/react-query";

import { useServices, useHealth } from "@/hooks/useServices";
import { ServiceCard } from "@/components/ServiceCard";
import { ServiceForm } from "@/components/ServiceForm";
import { useAppStore } from "@/store/useAppStore";

export function ServicesPage() {
  const qc = useQueryClient();
  const servicesQuery = useServices();
  const healthQuery = useHealth();
  const services = servicesQuery.data || [];
  const health = healthQuery.data || {};

  const { selectedServiceId, setSelectedServiceId } = useAppStore();

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div className="text-xl font-semibold">服务管理</div>
        <ServiceForm
          onRegistered={() => qc.invalidateQueries({ queryKey: ["services"] })}
        />
      </div>

      {servicesQuery.isLoading ? (
        <div className="text-sm text-muted-foreground">加载中...</div>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
          {services.map((s) => (
            <ServiceCard
              key={s.service_id}
              service={s}
              health={health[s.service_id]}
              selected={selectedServiceId === s.service_id}
              onSelect={() => setSelectedServiceId(s.service_id)}
            />
          ))}
        </div>
      )}
      {selectedServiceId && (
        <div className="text-xs text-muted-foreground">
          已选择: {selectedServiceId}
        </div>
      )}
    </div>
  );
}
