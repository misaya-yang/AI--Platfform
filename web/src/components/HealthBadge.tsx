import { Badge } from "@/components/ui/badge";
import { useTranslation } from "react-i18next";

export function HealthBadge({ status }: { status?: string }) {
  const { t } = useTranslation();
  const statusMap: Record<string, string> = {
    healthy: t("health.healthy"),
    unhealthy: t("health.unhealthy"),
    error: t("health.error"),
    timeout: t("health.timeout"),
    unknown: t("health.unknown"),
  };
  const s = (status || "unknown").toLowerCase();
  const variant =
    s === "healthy"
      ? "default"
      : s === "unhealthy"
      ? "secondary"
      : s === "error" || s === "timeout"
      ? "destructive"
      : "outline";
  return <Badge variant={variant}>{statusMap[s] || s}</Badge>;
}
