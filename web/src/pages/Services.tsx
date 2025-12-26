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

  // 如果当前选中的服务已经不在列表中，重置选择
  if (selectedServiceId && services.length > 0 && !services.some(s => s.service_id === selectedServiceId)) {
    // 延迟执行以避免在渲染期间更新状态
    setTimeout(() => setSelectedServiceId(null), 0);
  }

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
      ) : services.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 border rounded-lg bg-muted/10 border-dashed">
          <p className="text-sm text-muted-foreground">暂无服务，请点击右上角添加</p>
        </div>
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
      {selectedServiceId && services.some(s => s.service_id === selectedServiceId) && (
        <div className="text-xs text-muted-foreground">
          已选择: {selectedServiceId}
        </div>
      )}
    </div>
  );
}
