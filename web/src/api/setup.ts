import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface SetupState {
  configured: boolean;
  missing: string[];
  mode: string;
  default_model: string | null;
}

export async function fetchSetupState(): Promise<SetupState> {
  const { data } = await api.get<SetupState>("/api/v1/setup/state");
  return data;
}

export function useSetupState() {
  return useQuery({
    queryKey: ["setup-state"],
    queryFn: fetchSetupState,
    staleTime: 5 * 60 * 1000, // 5 minutes — setup changes rarely
    refetchOnWindowFocus: true,
    retry: false,
  });
}
