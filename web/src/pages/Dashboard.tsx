import { useTranslation } from "react-i18next";
import { DatePicker, Select } from "antd";
import dayjs from "dayjs";
import { useState } from "react";

import { useServices, useHealth } from "@/hooks/useServices";
import { ServiceCard } from "@/components/ServiceCard";
import { ProviderStatusCard } from "@/components/ProviderStatusCard";
import { ServiceCostAnalysis } from "@/components/ServiceCostAnalysis";
import { UserServiceUsageAnalytics } from "@/components/UserServiceUsageAnalytics";
import { SecurityEventCharts } from "@/components/SecurityEventCharts";

const { RangePicker } = DatePicker;

export function DashboardPage() {
  const { t } = useTranslation();
  const servicesQuery = useServices();
  const healthQuery = useHealth();
  const services = servicesQuery.data || [];
  const health = healthQuery.data || {};
  const [dateRange, setDateRange] = useState<[dayjs.Dayjs, dayjs.Dayjs]>([
    dayjs().subtract(7, "day"),
    dayjs(),
  ]);
  const [granularity, setGranularity] = useState<"day" | "hour">("day");

  return (
    <div className="space-y-6">
      {/* 统一筛选 */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-xl font-semibold">{t("dashboard.title")}</div>
        <div className="flex items-center gap-2">
          <RangePicker
            value={dateRange}
            onChange={(value) => value && setDateRange(value as [dayjs.Dayjs, dayjs.Dayjs])}
          />
          <Select
            value={granularity}
            onChange={(value) => setGranularity(value)}
            options={[
              { value: "day", label: t("dashboard.granularity.day", "按天") },
              { value: "hour", label: t("dashboard.granularity.hour", "按小时") },
            ]}
            style={{ width: 120 }}
          />
        </div>
      </div>

      {/* 服务入口 */}
      <div>
        <h3 className="text-base font-medium mb-3">{t("dashboard.serviceEndpoints", "服务入口")}</h3>
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

      {/* 模型供应商状态 */}
      <ProviderStatusCard />

      {/* 服务成本分析 */}
      <ServiceCostAnalysis dateRange={dateRange} granularity={granularity} />

      {/* 按用户/服务历史统计 */}
      <UserServiceUsageAnalytics dateRange={dateRange} granularity={granularity} />

      {/* 鉴权失败/限流触发 */}
      <SecurityEventCharts dateRange={dateRange} granularity={granularity} />
    </div>
  );
}
