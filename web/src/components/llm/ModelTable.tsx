/**
 * Model Table Component
 *
 * Table for displaying and managing LLM models.
 */

import { useState } from "react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import { Eye, EyeOff, Wrench, Pencil, Trash2 } from "lucide-react";
import type { LLMModel } from "@/api/models";
import {
  formatContextWindow,
  formatPrice,
  getAccessLevelDisplayName,
  getAccessLevelColor,
} from "@/api/models";

interface ModelTableProps {
  models: LLMModel[];
  providers: Record<string, string>; // provider_id -> display_name
  onEdit?: (model: LLMModel) => void;
  onDelete?: (model: LLMModel) => void;
  onToggle?: (model: LLMModel, enabled: boolean) => void;
  loading?: boolean;
}

export function ModelTable({
  models,
  providers,
  onEdit,
  onDelete,
  onToggle,
  loading,
}: ModelTableProps) {
  const [togglingId, setTogglingId] = useState<string | null>(null);

  const handleToggle = async (model: LLMModel, enabled: boolean) => {
    if (!onToggle) return;
    setTogglingId(model.model_id);
    try {
      await onToggle(model, enabled);
    } finally {
      setTogglingId(null);
    }
  };

  if (models.length === 0) {
    return (
      <div className="text-center py-12 text-muted-foreground">
        暂无模型数据
      </div>
    );
  }

  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead className="w-[200px]">模型</TableHead>
            <TableHead>厂商</TableHead>
            <TableHead className="text-center">上下文</TableHead>
            <TableHead className="text-center">输出限制</TableHead>
            <TableHead className="text-center">价格</TableHead>
            <TableHead className="text-center">功能</TableHead>
            <TableHead className="text-center">权限</TableHead>
            <TableHead className="text-center">状态</TableHead>
            <TableHead className="text-right">操作</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {models.map((model) => (
            <TableRow
              key={model.model_id}
              className={cn(!model.is_enabled && "opacity-60")}
            >
              <TableCell>
                <div>
                  <div className="font-medium">{model.display_name}</div>
                  <div className="text-xs text-muted-foreground">
                    {model.model_id}
                  </div>
                </div>
              </TableCell>
              <TableCell>
                {providers[model.provider_id] || model.provider_id}
              </TableCell>
              <TableCell className="text-center">
                {formatContextWindow(model.context_window)}
              </TableCell>
              <TableCell className="text-center">
                {formatContextWindow(model.max_output_tokens)}
              </TableCell>
              <TableCell className="text-center">
                <div className="text-xs">
                  <div>入: {formatPrice(model.input_price_per_1k)}</div>
                  <div>出: {formatPrice(model.output_price_per_1k)}</div>
                </div>
              </TableCell>
              <TableCell className="text-center">
                <div className="flex items-center justify-center gap-1">
                  {model.supports_vision && (
                    <Badge variant="outline" className="text-xs">
                      <Eye className="h-3 w-3 mr-1" />
                      Vision
                    </Badge>
                  )}
                  {model.supports_tools && (
                    <Badge variant="outline" className="text-xs">
                      <Wrench className="h-3 w-3 mr-1" />
                      Tools
                    </Badge>
                  )}
                </div>
              </TableCell>
              <TableCell className="text-center">
                <Badge
                  variant="secondary"
                  className={cn(
                    "text-xs",
                    model.access_level === "admin" &&
                      "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300",
                    model.access_level === "premium" &&
                      "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
                    model.access_level === "public" &&
                      "bg-green-100 text-green-700 dark:bg-green-950 dark:text-green-300"
                  )}
                >
                  {getAccessLevelDisplayName(model.access_level)}
                </Badge>
              </TableCell>
              <TableCell className="text-center">
                <Switch
                  checked={model.is_enabled}
                  onCheckedChange={(checked) => handleToggle(model, checked)}
                  disabled={loading || togglingId === model.model_id}
                />
              </TableCell>
              <TableCell className="text-right">
                <div className="flex items-center justify-end gap-1">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8"
                    onClick={() => onEdit?.(model)}
                  >
                    <Pencil className="h-4 w-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-destructive hover:text-destructive"
                    onClick={() => onDelete?.(model)}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
