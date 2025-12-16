import { useQuery } from "@tanstack/react-query";
import { getHealth, listServices } from "@/api/gateway";

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

