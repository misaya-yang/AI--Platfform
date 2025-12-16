import { Badge } from "@/components/ui/badge";

const statusMap: Record<string, string> = {
  healthy: "健康",
  unhealthy: "异常",
  error: "错误",
  timeout: "超时",
  unknown: "未知",
};

export function HealthBadge({ status }: { status?: string }) {
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
