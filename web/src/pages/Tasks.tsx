import { useState } from "react";

import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { useTask, useTaskResult } from "@/hooks/useTasks";

export function TasksPage() {
  const [taskId, setTaskId] = useState("");
  const taskQuery = useTask(taskId || undefined);
  const resultQuery = useTaskResult(taskId || undefined);

  return (
    <div className="space-y-4">
      <div className="text-xl font-semibold">任务管理</div>
      <div className="flex gap-2">
        <Input
          placeholder="输入任务 ID"
          value={taskId}
          onChange={(e) => setTaskId(e.target.value)}
        />
        <Button size="sm" onClick={() => taskQuery.refetch()}>
          查询
        </Button>
      </div>

      {taskQuery.data && (
        <div className="rounded-md border bg-card p-3 text-sm space-y-1">
          <div>服务: {taskQuery.data.service_id}</div>
          <div>状态: {taskQuery.data.status}</div>
          <div>创建时间: {taskQuery.data.created_at}</div>
          {taskQuery.data.error && (
            <div className="text-destructive">
              错误: {taskQuery.data.error}
            </div>
          )}
        </div>
      )}

      {resultQuery.data && (
        <pre className="rounded-md border bg-card p-3 text-xs whitespace-pre-wrap">
          {JSON.stringify(resultQuery.data, null, 2)}
        </pre>
      )}
    </div>
  );
}
