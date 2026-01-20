/**
 * Add Confluence Binding Dialog
 *
 * Dialog for binding a Confluence space to a dataset.
 * Adapted from BindSpace.tsx but as a dialog form.
 *
 * Flow:
 * 1. Select Confluence connection
 * 2. Select space and pages
 * 3. Configure sync options
 */

import { useState, useMemo, useCallback, useEffect } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Folder,
  FolderOpen,
  FileText,
  ChevronDown,
  CheckCircle,
  Loader2,
  AlertCircle,
  Settings2,
  Cloud,
  Square,
  CheckSquare,
  MinusSquare,
  Info,
  ImageIcon,
  ArrowLeft,
  ArrowRight,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { toast } from "@/hooks/use-toast";

import {
  listConnections,
  discoverSpaces,
  discoverSpacePages,
  createBinding,
} from "@/api/confluence";
import type { ConfluencePageTreeNode, ConfluenceBindingCreateRequest } from "@/types/confluence";
import { cn } from "@/lib/utils";

// ============================================================
// Types
// ============================================================

interface SelectedPage {
  pageId: string;
  title: string;
  hasChildren: boolean;
}

interface AddConfluenceBindingDialogProps {
  datasetId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated?: () => void;
}

// ============================================================
// Page Tree Component
// ============================================================

interface PageTreeNodeProps {
  node: ConfluencePageTreeNode;
  selectedPages: Map<string, SelectedPage>;
  onToggle: (pageId: string, title: string, hasChildren: boolean) => void;
  level?: number;
  parentSelected?: boolean;
}

function PageTreeNodeComponent({
  node,
  selectedPages,
  onToggle,
  level = 0,
  parentSelected = false,
}: PageTreeNodeProps) {
  const [expanded, setExpanded] = useState(level < 2);
  const isSelected = selectedPages.has(node.page_id);
  const isFolder = node.has_children;

  const hasSelectedChildren = useMemo(() => {
    if (!node.children || node.children.length === 0) return false;
    const checkChildren = (children: ConfluencePageTreeNode[]): boolean => {
      return children.some(
        (child) =>
          selectedPages.has(child.page_id) ||
          (child.children && checkChildren(child.children))
      );
    };
    return checkChildren(node.children);
  }, [node.children, selectedPages]);

  const checkboxState: "checked" | "partial" | "unchecked" = isSelected
    ? "checked"
    : hasSelectedChildren
    ? "partial"
    : "unchecked";

  return (
    <div className="select-none">
      <div
        className={cn(
          "flex items-center gap-1.5 px-2 py-1.5 rounded-md transition-all",
          "hover:bg-muted/60 cursor-pointer group",
          isSelected && "bg-primary/8 hover:bg-primary/12",
          parentSelected && "opacity-60"
        )}
        style={{ paddingLeft: `${level * 16 + 8}px` }}
      >
        {isFolder ? (
          <button
            onClick={(e) => {
              e.stopPropagation();
              setExpanded(!expanded);
            }}
            className="p-0.5 hover:bg-muted rounded transition-colors"
          >
            <ChevronDown
              className={cn(
                "h-4 w-4 text-muted-foreground transition-transform duration-200",
                !expanded && "-rotate-90"
              )}
            />
          </button>
        ) : (
          <span className="w-5" />
        )}

        <button
          onClick={(e) => {
            e.stopPropagation();
            onToggle(node.page_id, node.title, node.has_children);
          }}
          className="p-0.5 rounded transition-colors hover:bg-primary/10"
        >
          {checkboxState === "checked" ? (
            <CheckSquare className="h-4 w-4 text-primary" />
          ) : checkboxState === "partial" ? (
            <MinusSquare className="h-4 w-4 text-primary/60" />
          ) : (
            <Square className="h-4 w-4 text-muted-foreground group-hover:text-foreground" />
          )}
        </button>

        {isFolder ? (
          expanded ? (
            <FolderOpen className="h-4 w-4 text-amber-500 flex-shrink-0" />
          ) : (
            <Folder className="h-4 w-4 text-amber-500 flex-shrink-0" />
          )
        ) : (
          <FileText className="h-4 w-4 text-blue-500 flex-shrink-0" />
        )}

        <span
          className={cn(
            "text-sm truncate flex-1",
            isSelected ? "text-primary font-medium" : "text-foreground"
          )}
          onClick={() => onToggle(node.page_id, node.title, node.has_children)}
        >
          {node.title}
        </span>

        {isFolder && node.children && node.children.length > 0 && (
          <Badge
            variant="outline"
            className="text-xs h-5 px-1.5 font-normal text-muted-foreground"
          >
            {node.children.length}
          </Badge>
        )}
      </div>

      {expanded && node.children && node.children.length > 0 && (
        <div className="animate-in slide-in-from-top-1 duration-200">
          {node.children.map((child) => (
            <PageTreeNodeComponent
              key={child.page_id}
              node={child}
              selectedPages={selectedPages}
              onToggle={onToggle}
              level={level + 1}
              parentSelected={isSelected || parentSelected}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ============================================================
// Step Indicator
// ============================================================

function StepIndicator({
  currentStep,
  steps,
}: {
  currentStep: number;
  steps: { label: string; icon: React.ReactNode }[];
}) {
  return (
    <div className="flex items-center justify-center gap-2 mb-6">
      {steps.map((step, index) => {
        const isActive = index === currentStep;
        const isCompleted = index < currentStep;

        return (
          <div key={index} className="flex items-center">
            {index > 0 && (
              <div
                className={cn(
                  "w-6 h-0.5 mx-1.5 transition-colors",
                  isCompleted ? "bg-primary" : "bg-border"
                )}
              />
            )}
            <div
              className={cn(
                "flex items-center gap-1.5 px-2.5 py-1 rounded-full transition-all text-xs",
                isActive
                  ? "bg-primary text-primary-foreground"
                  : isCompleted
                  ? "bg-primary/20 text-primary"
                  : "bg-muted text-muted-foreground"
              )}
            >
              {isCompleted ? <CheckCircle className="h-3.5 w-3.5" /> : step.icon}
              <span className="font-medium">{step.label}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ============================================================
// Depth Selector
// ============================================================

function DepthSelector({
  value,
  onChange,
}: {
  value: number;
  onChange: (depth: number) => void;
}) {
  const depths = [
    { value: 1, label: "1", desc: "仅当前页" },
    { value: 2, label: "2", desc: "2 层" },
    { value: 3, label: "3", desc: "3 层" },
    { value: 5, label: "5", desc: "5 层" },
    { value: 10, label: "10", desc: "10 层" },
    { value: 100, label: "All", desc: "全部" },
  ];

  return (
    <div className="grid grid-cols-6 gap-2">
      {depths.map((depth) => (
        <button
          key={depth.value}
          type="button"
          onClick={() => onChange(depth.value)}
          className={cn(
            "flex flex-col items-center gap-0.5 p-2 rounded-lg border transition-all",
            "hover:border-primary/50 hover:bg-primary/5",
            value === depth.value
              ? "border-primary bg-primary/10 text-primary"
              : "border-border text-muted-foreground"
          )}
        >
          <span className="text-sm font-semibold">{depth.label}</span>
          <span className="text-[10px]">{depth.desc}</span>
        </button>
      ))}
    </div>
  );
}

// ============================================================
// Main Dialog Component
// ============================================================

export function AddConfluenceBindingDialog({
  datasetId,
  open,
  onOpenChange,
  onCreated,
}: AddConfluenceBindingDialogProps) {
  const queryClient = useQueryClient();

  // Current step (0: connection, 1: space, 2: options)
  const [currentStep, setCurrentStep] = useState(0);

  // Form state
  const [selectedConnectionId, setSelectedConnectionId] = useState("");
  const [selectedSpaceKey, setSelectedSpaceKey] = useState("");
  const [selectedSpaceName, setSelectedSpaceName] = useState("");
  const [selectedPages, setSelectedPages] = useState<Map<string, SelectedPage>>(new Map());
  const [syncEntireSpace, setSyncEntireSpace] = useState(true);
  const [maxDepth, setMaxDepth] = useState(10);
  const [includeAttachments, setIncludeAttachments] = useState(false);
  const [includeComments] = useState(false);
  const [syncImages, setSyncImages] = useState(true);
  const [imageMaxSizeBytes] = useState(3 * 1024 * 1024);

  // Reset form when dialog closes
  const handleOpenChange = (open: boolean) => {
    if (!open) {
      // Reset state
      setCurrentStep(0);
      setSelectedConnectionId("");
      setSelectedSpaceKey("");
      setSelectedSpaceName("");
      setSelectedPages(new Map());
      setSyncEntireSpace(true);
      setMaxDepth(10);
    }
    onOpenChange(open);
  };

  // Queries
  const { data: connections = [], isLoading: loadingConnections, refetch: refetchConnections } = useQuery({
    queryKey: ["confluence-connections"],
    queryFn: () => listConnections(),
    enabled: open,
  });

  // Refresh connections when window gains focus (e.g., after creating connection in new tab)
  useEffect(() => {
    if (!open) return;

    const handleFocus = () => {
      refetchConnections();
    };

    window.addEventListener("focus", handleFocus);
    return () => window.removeEventListener("focus", handleFocus);
  }, [open, refetchConnections]);

  const {
    data: spacesResponse,
    isLoading: loadingSpaces,
    error: spacesError,
  } = useQuery({
    queryKey: ["confluence-spaces", selectedConnectionId],
    queryFn: () => discoverSpaces(selectedConnectionId),
    enabled: open && !!selectedConnectionId,
  });

  const { data: pageTreeResponse, isLoading: loadingPageTree } = useQuery({
    queryKey: ["confluence-page-tree", selectedConnectionId, selectedSpaceKey],
    queryFn: () => discoverSpacePages(selectedConnectionId, selectedSpaceKey, 5),
    enabled: open && !!selectedConnectionId && !!selectedSpaceKey,
  });

  // Create binding mutation
  const createBindingMutation = useMutation({
    mutationFn: (data: ConfluenceBindingCreateRequest) =>
      createBinding(selectedConnectionId, data),
    onSuccess: () => {
      toast.success("绑定创建成功", "Confluence 空间已绑定到知识库");
      queryClient.invalidateQueries({ queryKey: ["kb-confluence-bindings", datasetId] });
      handleOpenChange(false);
      onCreated?.();
    },
    onError: (error) => {
      const message = error instanceof Error ? error.message : String(error);
      if (message.includes("already exists")) {
        toast.error("绑定已存在", "该空间已绑定到此知识库");
      } else {
        toast.error("创建失败", message);
      }
    },
  });

  // Filter active connections
  const activeConnections = useMemo(() => {
    return connections.filter((c) => c.status === "active");
  }, [connections]);

  // Get spaces list
  const spaces = spacesResponse?.spaces || [];

  // Handle connection selection
  const handleConnectionSelect = (value: string) => {
    setSelectedConnectionId(value);
    setSelectedSpaceKey("");
    setSelectedSpaceName("");
    setSelectedPages(new Map());
    setSyncEntireSpace(true);
  };

  // Handle space selection
  const handleSpaceSelect = (value: string) => {
    const space = spaces.find((s) => s.space_key === value);
    setSelectedSpaceKey(value);
    setSelectedSpaceName(space?.name || value);
    setSelectedPages(new Map());
    setSyncEntireSpace(true);
  };

  // Handle page toggle
  const handlePageToggle = useCallback(
    (pageId: string, title: string, hasChildren: boolean) => {
      setSelectedPages((prev) => {
        const newMap = new Map(prev);
        if (newMap.has(pageId)) {
          newMap.delete(pageId);
          if (newMap.size === 0) {
            setSyncEntireSpace(true);
          }
        } else {
          newMap.set(pageId, { pageId, title, hasChildren });
          setSyncEntireSpace(false);
        }
        return newMap;
      });
    },
    []
  );

  // Check if can proceed
  const canProceed = () => {
    switch (currentStep) {
      case 0:
        return !!selectedConnectionId;
      case 1:
        return !!selectedSpaceKey;
      case 2:
        return true;
      default:
        return false;
    }
  };

  // Handle create binding
  const handleCreate = () => {
    if (!selectedSpaceKey) return;

    const rootPageIds = syncEntireSpace
      ? undefined
      : Array.from(selectedPages.values()).map((p) => p.pageId);

    createBindingMutation.mutate({
      dataset_id: datasetId,
      space_key: selectedSpaceKey,
      root_page_ids: rootPageIds,
      max_depth: maxDepth,
      include_attachments: includeAttachments,
      include_comments: includeComments,
      sync_images: syncImages,
      image_max_size_bytes: imageMaxSizeBytes,
    });
  };

  const steps = [
    { label: "连接", icon: <Cloud className="h-3.5 w-3.5" /> },
    { label: "空间", icon: <Folder className="h-3.5 w-3.5" /> },
    { label: "选项", icon: <Settings2 className="h-3.5 w-3.5" /> },
  ];

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent className="max-w-2xl max-h-[85vh] overflow-hidden flex flex-col">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Cloud className="h-5 w-5 text-blue-500" />
            添加 Confluence 绑定
          </DialogTitle>
        </DialogHeader>

        <StepIndicator currentStep={currentStep} steps={steps} />

        <div className="flex-1 overflow-y-auto px-1">
          {/* Step 0: Select Connection */}
          {currentStep === 0 && (
            <div className="space-y-4">
              <div className="flex items-start gap-3 p-3 bg-muted/30 rounded-lg border border-border/50">
                <Info className="h-4 w-4 text-muted-foreground flex-shrink-0 mt-0.5" />
                <p className="text-sm text-muted-foreground">
                  选择一个已配置的 Confluence 连接来同步页面
                </p>
              </div>

              <div className="space-y-2">
                <Label>Confluence 连接</Label>
                {loadingConnections ? (
                  <div className="flex items-center justify-center h-10 border rounded-lg">
                    <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                  </div>
                ) : activeConnections.length > 0 ? (
                  <Select value={selectedConnectionId} onValueChange={handleConnectionSelect}>
                    <SelectTrigger>
                      <SelectValue placeholder="选择连接...">
                        {selectedConnectionId && (() => {
                          const conn = activeConnections.find(c => c.connection_id === selectedConnectionId);
                          return conn ? (
                            <div className="flex items-center gap-2">
                              <Cloud className="h-4 w-4 text-blue-500" />
                              <span>{conn.name}</span>
                              <span className="text-xs text-muted-foreground">({conn.domain})</span>
                            </div>
                          ) : "选择连接...";
                        })()}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      {activeConnections.map((conn) => (
                        <SelectItem key={conn.connection_id} value={conn.connection_id}>
                          {conn.name} ({conn.domain})
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                ) : (
                  <div className="p-4 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-900 rounded-lg">
                    <p className="text-sm text-amber-700 dark:text-amber-400 mb-3">
                      还没有可用的 Confluence 连接
                    </p>
                    <Button variant="outline" size="sm" asChild>
                      <Link to="/confluence/connections/new" target="_blank">
                        <Cloud className="h-4 w-4 mr-1.5" />
                        创建连接
                      </Link>
                    </Button>
                  </div>
                )}
              </div>

              {activeConnections.length > 0 && (
                <div className="flex justify-end">
                  <Link
                    to="/confluence/connections/new"
                    target="_blank"
                    className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground transition-colors"
                  >
                    <Cloud className="h-3.5 w-3.5 mr-1" />
                    新建连接
                  </Link>
                </div>
              )}
            </div>
          )}

          {/* Step 1: Select Space & Pages */}
          {currentStep === 1 && (
            <div className="space-y-4">
              {spacesError ? (
                <div className="p-4 bg-rose-50 dark:bg-rose-950/30 border border-rose-200 dark:border-rose-900 rounded-lg">
                  <div className="flex items-center gap-2 text-rose-700 dark:text-rose-400">
                    <AlertCircle className="h-5 w-5" />
                    <span className="font-medium">无法加载空间</span>
                  </div>
                </div>
              ) : (
                <>
                  {/* Space Selector */}
                  <div className="space-y-2">
                    <Label>选择空间</Label>
                    {loadingSpaces ? (
                      <div className="flex items-center justify-center h-10 border rounded-lg">
                        <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                      </div>
                    ) : spaces.length > 0 ? (
                      <Select value={selectedSpaceKey} onValueChange={handleSpaceSelect}>
                        <SelectTrigger>
                          <SelectValue placeholder="选择空间...">
                            {selectedSpaceKey && (() => {
                              const space = spaces.find(s => s.space_key === selectedSpaceKey);
                              return space ? (
                                <div className="flex items-center gap-2">
                                  <Folder className="h-4 w-4 text-amber-500" />
                                  <span>{space.name}</span>
                                  <span className="text-xs text-muted-foreground">({space.space_key})</span>
                                </div>
                              ) : "选择空间...";
                            })()}
                          </SelectValue>
                        </SelectTrigger>
                        <SelectContent className="max-h-[300px]">
                          {spaces.map((space) => (
                            <SelectItem key={space.space_key} value={space.space_key}>
                              {space.name} ({space.space_key})
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    ) : (
                      <div className="p-4 text-center text-muted-foreground text-sm border rounded-lg">
                        该连接下没有可用的空间
                      </div>
                    )}
                  </div>

                  {/* Page Tree */}
                  {selectedSpaceKey && (
                    <div className="space-y-2">
                      <div className="flex items-center justify-between">
                        <Label>选择要同步的页面</Label>
                        {selectedPages.size > 0 && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => {
                              setSelectedPages(new Map());
                              setSyncEntireSpace(true);
                            }}
                          >
                            清除选择
                          </Button>
                        )}
                      </div>

                      <div className="flex items-center gap-2 mb-2">
                        <Badge variant={syncEntireSpace ? "default" : "outline"}>
                          {syncEntireSpace
                            ? "同步整个空间"
                            : `已选 ${selectedPages.size} 个根页面`}
                        </Badge>
                      </div>

                      <div className="border rounded-lg max-h-[300px] overflow-y-auto bg-muted/20">
                        {loadingPageTree ? (
                          <div className="flex items-center justify-center p-8">
                            <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                          </div>
                        ) : pageTreeResponse?.root_pages?.length ? (
                          <div className="p-2">
                            {pageTreeResponse.root_pages.map((node) => (
                              <PageTreeNodeComponent
                                key={node.page_id}
                                node={node}
                                selectedPages={selectedPages}
                                onToggle={handlePageToggle}
                              />
                            ))}
                          </div>
                        ) : (
                          <div className="p-8 text-center text-muted-foreground text-sm">
                            该空间没有页面
                          </div>
                        )}
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* Step 2: Options */}
          {currentStep === 2 && (
            <div className="space-y-6">
              {/* Summary */}
              <div className="p-4 bg-muted/30 rounded-lg border border-border/50">
                <div className="text-sm text-muted-foreground mb-2">即将绑定:</div>
                <div className="flex items-center gap-2">
                  <Folder className="h-4 w-4 text-amber-500" />
                  <span className="font-medium">{selectedSpaceName}</span>
                  <Badge variant="outline" className="text-xs">
                    {syncEntireSpace
                      ? "整个空间"
                      : `${selectedPages.size} 个根页面`}
                  </Badge>
                </div>
              </div>

              {/* Depth */}
              <div className="space-y-3">
                <Label className="flex items-center gap-2">
                  同步深度
                  <Badge variant="secondary" className="text-xs font-normal">
                    当前: {maxDepth === 100 ? "全部" : maxDepth + " 层"}
                  </Badge>
                </Label>
                <DepthSelector value={maxDepth} onChange={setMaxDepth} />
              </div>

              {/* Image sync */}
              <div className="flex items-center justify-between p-3 border rounded-lg">
                <div className="flex items-center gap-3">
                  <ImageIcon className="h-5 w-5 text-muted-foreground" />
                  <div>
                    <Label className="text-sm font-medium">同步图片</Label>
                    <p className="text-xs text-muted-foreground">
                      同步页面中的图片并进行 VLM 描述
                    </p>
                  </div>
                </div>
                <Switch checked={syncImages} onCheckedChange={setSyncImages} />
              </div>

              {/* Attachments */}
              <div className="flex items-center justify-between p-3 border rounded-lg">
                <div className="flex items-center gap-3">
                  <FileText className="h-5 w-5 text-muted-foreground" />
                  <div>
                    <Label className="text-sm font-medium">同步附件</Label>
                    <p className="text-xs text-muted-foreground">
                      同步页面的附件文件
                    </p>
                  </div>
                </div>
                <Switch
                  checked={includeAttachments}
                  onCheckedChange={setIncludeAttachments}
                />
              </div>
            </div>
          )}
        </div>

        <DialogFooter className="gap-2 sm:gap-2">
          {currentStep > 0 && (
            <Button
              variant="outline"
              onClick={() => setCurrentStep((s) => s - 1)}
              disabled={createBindingMutation.isPending}
            >
              <ArrowLeft className="h-4 w-4 mr-1.5" />
              上一步
            </Button>
          )}
          {currentStep < 2 ? (
            <Button onClick={() => setCurrentStep((s) => s + 1)} disabled={!canProceed()}>
              下一步
              <ArrowRight className="h-4 w-4 ml-1.5" />
            </Button>
          ) : (
            <Button
              onClick={handleCreate}
              disabled={createBindingMutation.isPending}
            >
              {createBindingMutation.isPending ? (
                <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
              ) : (
                <CheckCircle className="h-4 w-4 mr-1.5" />
              )}
              创建绑定
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
