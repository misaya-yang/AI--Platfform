import { useQuery } from "@tanstack/react-query";
import { getTask, getTaskResult, listTasks } from "@/api/gateway";

export function useTasks(status?: string) {
  return useQuery({
    queryKey: ["tasks", { status }],
    queryFn: () => listTasks({ status, limit: 50 }),
    refetchInterval: (query) => {
      const tasks = query.state.data ?? [];
      return tasks.some((task) => ["pending", "processing"].includes(task.status))
        ? 3_000
        : 15_000;
    },
  });
}

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
