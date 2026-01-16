/**
 * Provider Card Component
 *
 * Displays a provider card with status, API key indicator, and actions.
 */

import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  Cloud,
  Key,
  Settings,
  Trash2,
  Loader2,
  CheckCircle2,
  XCircle,
  Zap,
} from "lucide-react";
import type { Provider } from "@/api/providers";
import { testProviderConnection, getApiTypeDisplayName } from "@/api/providers";

interface ProviderCardProps {
  provider: Provider;
  onEdit?: () => void;
  onDelete?: () => void;
}

export function ProviderCard({ provider, onEdit, onDelete }: ProviderCardProps) {
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{
    success: boolean;
    message: string;
    latency_ms?: number;
  } | null>(null);

  const handleTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await testProviderConnection(provider.provider_id);
      setTestResult(result);
    } catch (error) {
      setTestResult({
        success: false,
        message: error instanceof Error ? error.message : "Connection test failed",
      });
    } finally {
      setTesting(false);
    }
  };

  return (
    <Card className={cn("relative overflow-hidden", !provider.is_enabled && "opacity-60")}>
      <CardContent className="p-5">
        <div className="flex items-start gap-4">
          <div className="text-muted-foreground">
            <Cloud className="h-6 w-6" />
          </div>

          <div className="flex-1 min-w-0">
            <h3 className="font-medium text-foreground truncate">
              {provider.display_name}
            </h3>
            <p className="text-sm text-muted-foreground mt-1">{provider.provider_id}</p>
            {provider.base_url && (
              <p className="text-xs text-muted-foreground mt-1 truncate">
                {provider.base_url}
              </p>
            )}
          </div>

          <div className="flex items-center gap-2">
            <Badge variant={provider.is_enabled ? "default" : "secondary"}>
              {provider.is_enabled ? "已启用" : "已禁用"}
            </Badge>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 mt-4">
          <Badge variant="secondary" className="text-xs">
            {getApiTypeDisplayName(provider.api_type)}
          </Badge>
          <Badge
            variant={provider.has_api_key ? "outline" : "destructive"}
            className="text-xs"
          >
            <Key className="h-3 w-3 mr-1" />
            {provider.has_api_key ? "已配置密钥" : "未配置密钥"}
          </Badge>
        </div>

        {/* Test Result */}
        {testResult && (
          <div
            className={cn(
              "mt-3 p-2 rounded-md text-sm flex items-center gap-2",
              testResult.success
                ? "bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-300"
                : "bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300"
            )}
          >
            {testResult.success ? (
              <CheckCircle2 className="h-4 w-4" />
            ) : (
              <XCircle className="h-4 w-4" />
            )}
            <span className="flex-1">{testResult.message}</span>
            {testResult.latency_ms && (
              <span className="text-xs opacity-70">{testResult.latency_ms}ms</span>
            )}
          </div>
        )}

        {/* Actions */}
        <div className="flex items-center gap-2 mt-4 pt-4 border-t">
          <Button
            variant="outline"
            size="sm"
            onClick={handleTest}
            disabled={testing || !provider.has_api_key}
          >
            {testing ? (
              <Loader2 className="h-4 w-4 mr-1 animate-spin" />
            ) : (
              <Zap className="h-4 w-4 mr-1" />
            )}
            测试连接
          </Button>
          <Button variant="outline" size="sm" onClick={onEdit}>
            <Settings className="h-4 w-4 mr-1" />
            编辑
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={onDelete}
            className="text-destructive hover:text-destructive"
          >
            <Trash2 className="h-4 w-4 mr-1" />
            删除
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
