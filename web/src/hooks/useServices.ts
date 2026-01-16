import { useQuery } from "@tanstack/react-query";
import { getHealth, listServices, getProvidersHealth } from "@/api/gateway";

export function useServices() {
  return useQuery({
    queryKey: ["services"],
    queryFn: listServices,
  });
}

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    refetchInterval: 30000,
  });
}

export function useProvidersHealth() {
  return useQuery({
    queryKey: ["providers-health"],
    queryFn: getProvidersHealth,
    refetchInterval: 60000, // 每分钟刷新
  });
}

