import { useState, useCallback } from "react";
import {
  Search,
  Loader2,
  AlertCircle,
  CheckCircle2,
  Clock,
  XCircle,
  RefreshCw,
  FileSearch,
  ArrowRight,
  Copy,
  Check,
} from "lucide-react";

import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useTask, useTaskResult } from "@/hooks/useTasks";

// 任务状态配置
const STATUS_CONFIG: Record<string, { label: string; variant: "default" | "secondary" | "destructive" | "outline"; icon: React.ReactNode; color: string }> = {
  completed: {
    label: "已完成",
    variant: "default",
    icon: <CheckCircle2 className="h-3.5 w-3.5" />,
    color: "bg-emerald-500/10 text-emerald-600 border-emerald-500/20",
  },
  running: {
    label: "运行中",
    variant: "secondary",
    icon: <Loader2 className="h-3.5 w-3.5 animate-spin" />,
    color: "bg-blue-500/10 text-blue-600 border-blue-500/20",
  },
  pending: {
    label: "等待中",
    variant: "outline",
    icon: <Clock className="h-3.5 w-3.5" />,
    color: "bg-amber-500/10 text-amber-600 border-amber-500/20",
  },
  failed: {
    label: "失败",
    variant: "destructive",
    icon: <XCircle className="h-3.5 w-3.5" />,
    color: "bg-red-500/10 text-red-600 border-red-500/20",
  },
  cancelled: {
    label: "已取消",
    variant: "outline",
    icon: <XCircle className="h-3.5 w-3.5" />,
    color: "bg-gray-500/10 text-gray-500 border-gray-500/20",
  },
};

function StatusBadge({ status }: { status: string }) {
  const config = STATUS_CONFIG[status] || {
    label: status,
    variant: "outline" as const,
    icon: <Clock className="h-3.5 w-3.5" />,
    color: "bg-gray-500/10 text-gray-500 border-gray-500/20",
  };

  return (
    <Badge className={`gap-1.5 ${config.color} border font-medium`}>
      {config.icon}
      {config.label}
    </Badge>
  );
}

export function TasksPage() {
  const [inputValue, setInputValue] = useState("");
  const [searchedId, setSearchedId] = useState<string | undefined>(undefined);
  const [copied, setCopied] = useState(false);

  const taskQuery = useTask(searchedId);
  const resultQuery = useTaskResult(searchedId);

  const handleSearch = useCallback(() => {
    const trimmed = inputValue.trim();
    if (!trimmed) return;
    setSearchedId(trimmed);
  }, [inputValue]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key === "Enter") {
        handleSearch();
      }
    },
    [handleSearch]
  );

  const handleCopyId = useCallback(() => {
    if (searchedId) {
      navigator.clipboard.writeText(searchedId);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }, [searchedId]);

  const hasSearched = searchedId !== undefined;
  const isLoading = taskQuery.isLoading || taskQuery.isFetching;
  const hasError = taskQuery.isError;
  const hasData = !!taskQuery.data;
  const isEmpty = hasSearched && !isLoading && !hasError && !hasData;

  return (
    <div className="space-y-6">
      {/* 页面标题 */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">任务管理</h1>
          <p className="text-sm text-muted-foreground mt-1">
            查询和追踪异步任务的执行状态
          </p>
        </div>
      </div>

      {/* 搜索区域 */}
      <Card className="overflow-hidden">
        <CardContent className="p-4">
          <div className="flex gap-3">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground/60" />
              <Input
                className="pl-9 h-10 bg-background"
                placeholder="输入任务 ID 进行查询..."
                value={inputValue}
                onChange={(e) => setInputValue(e.target.value)}
                onKeyDown={handleKeyDown}
              />
            </div>
            <Button
              onClick={handleSearch}
              disabled={!inputValue.trim() || isLoading}
              className="h-10 px-5"
            >
              {isLoading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  查询中
                </>
              ) : (
                <>
                  查询
                  <ArrowRight className="ml-2 h-4 w-4" />
                </>
              )}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Loading 状态 */}
      {isLoading && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-16">
            <div className="relative">
              <div className="absolute inset-0 rounded-full bg-primary/10 animate-ping" />
              <div className="relative rounded-full bg-primary/10 p-4">
                <Loader2 className="h-8 w-8 text-primary animate-spin" />
              </div>
            </div>
            <p className="mt-4 text-sm font-medium text-foreground">正在查询任务...</p>
            <p className="text-xs text-muted-foreground mt-1">任务 ID: {searchedId}</p>
          </CardContent>
        </Card>
      )}

      {/* Error 状态 */}
      {hasError && !isLoading && (
        <Card className="border-destructive/30 bg-destructive/5">
          <CardContent className="flex flex-col items-center justify-center py-12">
            <div className="rounded-full bg-destructive/10 p-4">
              <AlertCircle className="h-8 w-8 text-destructive" />
            </div>
            <p className="mt-4 text-sm font-medium text-destructive">查询失败</p>
            <p className="text-xs text-muted-foreground mt-1 max-w-md text-center">
              {taskQuery.error instanceof Error
                ? taskQuery.error.message
                : "无法获取任务信息，请检查任务 ID 是否正确或稍后重试"}
            </p>
            <Button
              variant="outline"
              size="sm"
              className="mt-4"
              onClick={() => taskQuery.refetch()}
            >
              <RefreshCw className="mr-2 h-3.5 w-3.5" />
              重试
            </Button>
          </CardContent>
        </Card>
      )}

      {/* Empty 状态 */}
      {isEmpty && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-16">
            <div className="rounded-full bg-muted p-4">
              <FileSearch className="h-8 w-8 text-muted-foreground/60" />
            </div>
            <p className="mt-4 text-sm font-medium text-foreground">未找到任务</p>
            <p className="text-xs text-muted-foreground mt-1">
              任务 ID "{searchedId}" 不存在或已过期
            </p>
          </CardContent>
        </Card>
      )}

      {/* 任务详情 */}
      {hasData && !isLoading && (
        <div className="space-y-4">
          {/* 任务信息卡片 */}
          <Card>
            <CardHeader className="pb-3">
              <div className="flex items-start justify-between">
                <div className="space-y-1">
                  <CardTitle className="text-base">任务详情</CardTitle>
                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <span className="font-mono">{searchedId}</span>
                    <button
                      onClick={handleCopyId}
                      className="p-1 hover:bg-muted rounded transition-colors"
                      title="复制任务ID"
                    >
                      {copied ? (
                        <Check className="h-3 w-3 text-green-500" />
                      ) : (
                        <Copy className="h-3 w-3" />
                      )}
                    </button>
                  </div>
                </div>
                <StatusBadge status={taskQuery.data.status} />
              </div>
            </CardHeader>
            <CardContent className="pt-0">
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-1">
                  <p className="text-xs text-muted-foreground">服务</p>
                  <p className="text-sm font-medium">{taskQuery.data.service_id}</p>
                </div>
                <div className="space-y-1">
                  <p className="text-xs text-muted-foreground">创建时间</p>
                  <p className="text-sm font-medium">
                    {new Date(taskQuery.data.created_at).toLocaleString("zh-CN")}
                  </p>
                </div>
              </div>

              {/* 错误信息 */}
              {taskQuery.data.error && (
                <div className="mt-4 p-3 rounded-lg bg-destructive/10 border border-destructive/20">
                  <div className="flex items-start gap-2">
                    <AlertCircle className="h-4 w-4 text-destructive mt-0.5 flex-shrink-0" />
                    <div>
                      <p className="text-xs font-medium text-destructive">错误信息</p>
                      <p className="text-xs text-destructive/80 mt-1">
                        {taskQuery.data.error}
                      </p>
                    </div>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>

          {/* 任务结果 */}
          {resultQuery.data && (
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="text-base">执行结果</CardTitle>
              </CardHeader>
              <CardContent className="pt-0">
                <pre className="rounded-lg bg-muted/50 border p-4 text-xs font-mono whitespace-pre-wrap overflow-auto max-h-96">
                  {JSON.stringify(resultQuery.data, null, 2)}
                </pre>
              </CardContent>
            </Card>
          )}
        </div>
      )}

      {/* 初始状态提示 */}
      {!hasSearched && (
        <Card className="border-dashed">
          <CardContent className="flex flex-col items-center justify-center py-16">
            <div className="rounded-full bg-primary/5 p-4">
              <Search className="h-8 w-8 text-primary/40" />
            </div>
            <p className="mt-4 text-sm font-medium text-foreground">输入任务 ID 开始查询</p>
            <p className="text-xs text-muted-foreground mt-1">
              支持查询异步任务的状态和执行结果
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
