import { useQuery } from "@tanstack/react-query";
import { getTask, getTaskResult } from "@/api/gateway";

export function useTask(taskId?: string) {
  return useQuery({
    queryKey: ["task", taskId],
    queryFn: () => getTask(taskId!),
    enabled: !!taskId,
    refetchInterval: 5000,
  });
}

export function useTaskResult(taskId?: string) {
  return useQuery({
    queryKey: ["task-result", taskId],
    queryFn: () => getTaskResult(taskId!),
    enabled: !!taskId,
  });
}

