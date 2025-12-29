import React from "react";
import { AlertCircle, CheckCircle, File, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

type StatusBadgeProps = {
  status: string;
  error?: string;
  progress?: number;
};

export function StatusBadge({ status, error, progress }: StatusBadgeProps) {
  const s = (status || "").toLowerCase();

  const formatProgress = (p?: number) => {
    if (p === undefined || p === null) return "";
    return ` ${Math.round(p)}%`;
  };

  const configs: Record<
    string,
    { icon: React.ReactNode; label: string; className: string }
  > = {
    completed: {
      icon: <CheckCircle className="h-3 w-3" />,
      label: "已完成",
      className: "bg-emerald-50 text-emerald-700 border-emerald-200",
    },
    failed: {
      icon: <AlertCircle className="h-3 w-3" />,
      label: "失败",
      className: "bg-rose-50 text-rose-700 border-rose-200",
    },
    embedding: {
      icon: <Loader2 className="h-3 w-3 animate-spin" />,
      label: `向量化中${formatProgress(progress)}`,
      className: "bg-primary/10 text-primary border-primary/20",
    },
    segmenting: {
      icon: <Loader2 className="h-3 w-3 animate-spin" />,
      label: `分段中${formatProgress(progress)}`,
      className: "bg-amber-50 text-amber-700 border-amber-200",
    },
    parsing: {
      icon: <Loader2 className="h-3 w-3 animate-spin" />,
      label: `解析中${formatProgress(progress)}`,
      className: "bg-accent/10 text-accent border-accent/20",
    },
  };

  const config = configs[s] || {
    icon: <File className="h-3 w-3" />,
    label: "已上传",
    className: "bg-muted/60 text-muted-foreground border-border",
  };

  const badge = (
    <Badge
      variant="outline"
      className={`text-xs font-medium cursor-default ${config.className}`}
    >
      {config.icon}
      <span className="ml-1">{config.label}</span>
    </Badge>
  );

  if (s === "failed" && error) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>{badge}</TooltipTrigger>
          <TooltipContent>
            <p className="max-w-xs break-words text-xs">{error}</p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return badge;
}
