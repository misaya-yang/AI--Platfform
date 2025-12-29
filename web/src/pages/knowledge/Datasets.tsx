import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import {
  Plus,
  Search,
  RefreshCcw,
  Database,
  FileText,
  Copy,
  Check,
  MoreHorizontal,
  Table2,
  ImageIcon,
} from "lucide-react";

import { useDatasets } from "@/hooks/useKnowledge";
import { deleteDataset } from "@/api/knowledge";
import type { Dataset } from "@/types/knowledge";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Edit3, Trash2 } from "lucide-react";

// 知识库卡片组件 - 阿里云风格
function DatasetCard({
  dataset,
  onViewDetail,
  onHitTest,
  onEdit,
  onDelete
}: {
  dataset: Dataset;
  onViewDetail: () => void;
  onHitTest: () => void;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [dropdownOpen, setDropdownOpen] = useState(false);

  const copyId = (e: React.MouseEvent) => {
    e.stopPropagation();
    navigator.clipboard.writeText(dataset.dataset_id);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div
      className="relative bg-card rounded-xl border border-border p-5 hover:border-primary/30 hover:shadow-lg transition-all duration-200 group"
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      {/* Header */}
      <div className="flex items-start gap-3">
        <div className="w-10 h-10 rounded-lg bg-primary/10 border border-primary/20 flex items-center justify-center shadow-md">
          <Database className="h-5 w-5 text-primary" />
        </div>
        <div className="flex-1 min-w-0">
          <h3 className="font-bold text-foreground truncate text-lg">
            {dataset.name}
          </h3>
        </div>
      </div>

      {/* Info */}
      <div className="mt-4 space-y-2">
        <div className="flex items-center text-sm">
          <span className="text-muted-foreground/70 w-12">描述</span>
          <span className="text-muted-foreground truncate flex-1">
            {dataset.description || "-"}
          </span>
        </div>
        <div className="flex items-center text-sm">
          <span className="text-muted-foreground/70 w-12">ID</span>
          <span className="text-muted-foreground font-mono text-xs truncate flex-1">
            {dataset.dataset_id.slice(0, 12)}
          </span>
          <button
            onClick={copyId}
            className="ml-1 p-1 hover:bg-muted/60 rounded transition-colors"
            title="复制ID"
          >
            {copied ? (
              <Check className="h-3.5 w-3.5 text-green-500" />
            ) : (
              <Copy className="h-3.5 w-3.5 text-muted-foreground/70" />
            )}
          </button>
        </div>
      </div>

      {/* Tags */}
      <div className="mt-4 pt-3 border-t border-border/60 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="text-xs bg-primary/10 text-primary border-primary/20">
            标准版
          </Badge>
          <Badge variant="outline" className="text-xs bg-secondary text-secondary-foreground border-border">
            基础问答
          </Badge>
        </div>
        <div className="flex items-center gap-1.5 text-muted-foreground/70">
          <FileText className="h-4 w-4" />
          <span className="text-sm font-medium">
            {dataset.statistics?.document_count ?? 0}
          </span>
        </div>
      </div>

      {/* Hover Actions - 阿里云风格 */}
      {(hovered || dropdownOpen) && (
        <div className="absolute inset-x-0 bottom-0 bg-card border-t border-border rounded-b-xl p-3 flex items-center gap-2 shadow-lg animate-in fade-in slide-in-from-bottom-2 duration-200">
          <Button
            onClick={onViewDetail}
            className="flex-1 bg-primary hover:bg-primary/90 text-white h-9"
          >
            查看详情
          </Button>
          <Button
            onClick={(e) => { e.stopPropagation(); onHitTest(); }}
            variant="outline"
            className="flex-1 h-9 border-border"
          >
            命中测试
          </Button>

          <DropdownMenu open={dropdownOpen} onOpenChange={setDropdownOpen}>
            <DropdownMenuTrigger asChild>
              <Button
                variant="outline"
                size="icon"
                className="h-9 w-9 border-border"
                onClick={(e) => e.stopPropagation()}
              >
                <MoreHorizontal className="h-4 w-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-[120px]">
              <DropdownMenuItem onClick={(e) => { e.stopPropagation(); setDropdownOpen(false); onEdit(); }}>
                <Edit3 className="mr-2 h-3.5 w-3.5 text-muted-foreground" />
                编辑
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onClick={(e) => { e.stopPropagation(); setDropdownOpen(false); onDelete(); }}
                className="text-red-600 focus:text-red-700 focus:bg-red-50"
              >
                <Trash2 className="mr-2 h-3.5 w-3.5" />
                删除
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      )}
    </div>
  );
}

export function KnowledgeDatasetsPage() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const datasetsQuery = useDatasets();
  const datasets = datasetsQuery.data || [];

  const [searchQuery, setSearchQuery] = useState("");
  const [visibilityFilter, setVisibilityFilter] = useState<string>("all");
  const [typeFilter, setTypeFilter] = useState<string>("all");

  const filteredDatasets = datasets.filter((d) => {
    const matchesSearch =
      d.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      d.dataset_id.toLowerCase().includes(searchQuery.toLowerCase()) ||
      (d.description || "").toLowerCase().includes(searchQuery.toLowerCase());
    const matchesVisibility = visibilityFilter === "all" || d.visibility === visibilityFilter;
    return matchesSearch && matchesVisibility;
  });

  // 顶部Tab类型
  const typeTabs = [
    { key: "all", label: "全部", icon: Database },
    { key: "document", label: "文档", icon: FileText },
    { key: "data", label: "数据", icon: Table2 },
    { key: "image", label: "图片", icon: ImageIcon },
  ];

  return (
    <div className="min-h-screen bg-background">
      {/* 顶部导航栏 - 阿里云风格 */}
      <div className="bg-card border-b border-border sticky top-0 z-20">
        <div className="max-w-[1400px] mx-auto px-6">
          <div className="flex items-center justify-between h-14">
            {/* 左侧：标题和Tab */}
            <div className="flex items-center gap-6">
              <div className="flex items-center gap-2">
                <h1 className="text-lg font-bold text-foreground">知识库</h1>
                <Badge variant="outline" className="text-xs font-mono">
                  {datasets.length}
                </Badge>
              </div>

              {/* 类型Tab */}
              <div className="flex items-center bg-secondary/60 rounded-lg p-1">
                {typeTabs.map((tab) => (
                  <button
                    key={tab.key}
                    onClick={() => setTypeFilter(tab.key)}
                    className={`
                      flex items-center gap-1.5 px-4 py-1.5 text-sm font-medium rounded-md transition-all
                      ${typeFilter === tab.key
                        ? "bg-card text-primary shadow-sm"
                        : "text-muted-foreground hover:text-foreground"
                      }
                    `}
                  >
                    <tab.icon className="h-4 w-4" />
                    {tab.label}
                  </button>
                ))}
              </div>
            </div>

            {/* 右侧：操作按钮 */}
            <div className="flex items-center gap-3">
              <Button
                variant="outline"
                size="icon"
                onClick={() => qc.invalidateQueries({ queryKey: ["kb-datasets"] })}
                className="h-9 w-9 bg-card"
                title="刷新列表"
              >
                <RefreshCcw className={`h-4 w-4 ${datasetsQuery.isFetching ? "animate-spin" : ""}`} />
              </Button>
              <Button
                onClick={() => nav("/knowledge/create")}
                className="bg-primary hover:bg-primary/90 text-white"
              >
                <Plus className="h-4 w-4 mr-2" />
                创建知识库
              </Button>
            </div>
          </div>
        </div>
      </div>

      {/* 主内容区 */}
      <div className="max-w-[1400px] mx-auto px-6 py-6">
        {/* 搜索和筛选栏 */}
        <div className="flex items-center justify-between mb-6">
          <div className="relative w-80">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground/70" />
            <Input
              placeholder="搜索知识库名称"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-10 bg-card border-border h-10"
            />
          </div>

          <div className="flex items-center gap-3">
            <Select value={visibilityFilter} onValueChange={setVisibilityFilter}>
              <SelectTrigger className="w-36 bg-card h-10">
                <SelectValue placeholder="可见性" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">全部类型</SelectItem>
                <SelectItem value="private">私有</SelectItem>
                <SelectItem value="tenant">租户</SelectItem>
                <SelectItem value="public">公开</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </div>

        {/* 知识库网格 */}
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
          {filteredDatasets.map((dataset) => (
            <DatasetCard
              key={dataset.dataset_id}
              dataset={dataset}
              onViewDetail={() => nav(`/knowledge/${dataset.dataset_id}`)}
              onHitTest={() => nav(`/knowledge/${dataset.dataset_id}?tab=retrieval`)}
              onEdit={() => nav(`/knowledge/${dataset.dataset_id}?tab=settings`)}
              onDelete={async () => {
                if (confirm(`确认删除知识库 "${dataset.name}" 吗？此操作不可恢复。`)) {
                  try {
                    await deleteDataset(dataset.dataset_id);
                    await qc.invalidateQueries({ queryKey: ["kb-datasets"] });
                  } catch (e) {
                    alert("删除失败: " + (e instanceof Error ? e.message : String(e)));
                  }
                }
              }}
            />
          ))}
        </div>

        {filteredDatasets.length === 0 && (
          <div className="text-center py-24">
            <div className="w-20 h-20 mx-auto rounded-2xl bg-primary/10 border border-primary/20 flex items-center justify-center mb-6">
              <Database className="h-10 w-10 text-primary/70" />
            </div>
            <h3 className="text-xl font-semibold text-foreground">暂无知识库</h3>
            <p className="text-muted-foreground mt-2 max-w-sm mx-auto">
              创建您的第一个知识库，上传文档或添加URL构建AI知识体系
            </p>
            <Button
              onClick={() => nav("/knowledge/create")}
              className="mt-6 bg-primary hover:bg-primary/90"
            >
              <Plus className="h-4 w-4 mr-2" />
              创建知识库
            </Button>
          </div>
        )}
      </div>

    </div>
  );
}
