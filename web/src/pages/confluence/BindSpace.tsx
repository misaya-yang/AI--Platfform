/**
 * Bind Confluence Space Page
 *
 * Wizard-style page for binding a Confluence space to a dataset.
 * Features:
 * - Searchable space selector using Combobox
 * - Hierarchical page tree with folder/page distinction
 * - Multi-select capability for pages
 * - Visual sync depth configuration
 */

import { useState, useMemo, useCallback } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Database,
  Folder,
  FolderOpen,
  FileText,
  ChevronRight,
  CheckCircle,
  Loader2,
  AlertCircle,
  Settings2,
  Link2,
  Search,
  FolderTree,
  ChevronDown,
  Square,
  CheckSquare,
  MinusSquare,
  Info,
  Layers,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Combobox } from "@/components/ui/combobox";
import type { ComboboxOption } from "@/components/ui/combobox";

import {
  getConnection,
  discoverSpaces,
  discoverSpacePages,
  createBinding,
} from "@/api/confluence";
import { listDatasets } from "@/api/knowledge";
import type {
  ConfluencePageTreeNode,
  ConfluenceBindingCreateRequest,
} from "@/types/confluence";
import { cn } from "@/lib/utils";

// ============================================================
// Types
// ============================================================

interface SelectedPage {
  pageId: string;
  title: string;
  hasChildren: boolean;
}

// ============================================================
// Page Tree Component with Multi-Select & Folder Distinction
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

  // Check if any child is selected (for partial state)
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

  // Determine checkbox state
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
        style={{ paddingLeft: `${level * 20 + 8}px` }}
      >
        {/* Expand/Collapse Button */}
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

        {/* Checkbox */}
        <button
          onClick={(e) => {
            e.stopPropagation();
            onToggle(node.page_id, node.title, node.has_children);
          }}
          className={cn(
            "p-0.5 rounded transition-colors",
            "hover:bg-primary/10"
          )}
        >
          {checkboxState === "checked" ? (
            <CheckSquare className="h-4 w-4 text-primary" />
          ) : checkboxState === "partial" ? (
            <MinusSquare className="h-4 w-4 text-primary/60" />
          ) : (
            <Square className="h-4 w-4 text-muted-foreground group-hover:text-foreground" />
          )}
        </button>

        {/* Icon - Folder or File */}
        {isFolder ? (
          expanded ? (
            <FolderOpen className="h-4 w-4 text-amber-500 flex-shrink-0" />
          ) : (
            <Folder className="h-4 w-4 text-amber-500 flex-shrink-0" />
          )
        ) : (
          <FileText className="h-4 w-4 text-blue-500 flex-shrink-0" />
        )}

        {/* Title */}
        <span
          className={cn(
            "text-sm truncate flex-1",
            isSelected ? "text-primary font-medium" : "text-foreground"
          )}
          onClick={() => onToggle(node.page_id, node.title, node.has_children)}
        >
          {node.title}
        </span>

        {/* Child count badge for folders */}
        {isFolder && node.children && node.children.length > 0 && (
          <Badge
            variant="outline"
            className="text-xs h-5 px-1.5 font-normal text-muted-foreground"
          >
            {node.children.length}
          </Badge>
        )}
      </div>

      {/* Children */}
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
// Step Indicator Component
// ============================================================

function StepIndicator({
  currentStep,
  steps,
}: {
  currentStep: number;
  steps: { label: string; icon: React.ReactNode }[];
}) {
  return (
    <div className="flex items-center justify-center gap-2 mb-8">
      {steps.map((step, index) => {
        const isActive = index === currentStep;
        const isCompleted = index < currentStep;

        return (
          <div key={index} className="flex items-center">
            {index > 0 && (
              <div
                className={cn(
                  "w-8 h-0.5 mx-2 transition-colors",
                  isCompleted ? "bg-primary" : "bg-border"
                )}
              />
            )}
            <div
              className={cn(
                "flex items-center gap-2 px-3 py-1.5 rounded-full transition-all",
                isActive
                  ? "bg-primary text-primary-foreground"
                  : isCompleted
                  ? "bg-primary/20 text-primary"
                  : "bg-muted text-muted-foreground"
              )}
            >
              {isCompleted ? (
                <CheckCircle className="h-4 w-4" />
              ) : (
                step.icon
              )}
              <span className="text-sm font-medium">{step.label}</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ============================================================
// Depth Visual Selector Component
// ============================================================

function DepthSelector({
  value,
  onChange,
  t,
}: {
  value: number;
  onChange: (depth: number) => void;
  t: (key: string) => string;
}) {
  const depths = [
    { value: 1, label: "1", desc: t("confluence.bind.depths.1") },
    { value: 2, label: "2", desc: t("confluence.bind.depths.2") },
    { value: 3, label: "3", desc: t("confluence.bind.depths.3") },
    { value: 5, label: "5", desc: t("confluence.bind.depths.5") },
    { value: 10, label: "10", desc: t("confluence.bind.depths.10") },
    { value: 100, label: "∞", desc: t("confluence.bind.depths.unlimited") },
  ];

  return (
    <div className="space-y-3">
      {/* Visual depth indicator */}
      <div className="flex items-end gap-1 h-12 px-2">
        {[1, 2, 3, 4, 5].map((level) => (
          <div
            key={level}
            className={cn(
              "flex-1 rounded-t transition-all duration-300",
              level <= (value > 5 ? 5 : value)
                ? "bg-gradient-to-t from-primary/80 to-primary/40"
                : "bg-muted/50"
            )}
            style={{ height: `${level * 18}%` }}
          />
        ))}
      </div>

      {/* Depth buttons */}
      <div className="grid grid-cols-6 gap-2">
        {depths.map((depth) => (
          <button
            key={depth.value}
            onClick={() => onChange(depth.value)}
            className={cn(
              "flex flex-col items-center gap-1 p-2 rounded-lg border transition-all",
              "hover:border-primary/50 hover:bg-primary/5",
              value === depth.value
                ? "border-primary bg-primary/10 text-primary"
                : "border-border text-muted-foreground"
            )}
          >
            <span className="text-lg font-semibold">{depth.label}</span>
            <span className="text-[10px] leading-tight text-center">
              {depth.desc.split(" ")[0]}
            </span>
          </button>
        ))}
      </div>

      {/* Description */}
      <div className="flex items-start gap-2 p-3 bg-muted/30 rounded-lg border border-border/50">
        <Info className="h-4 w-4 text-muted-foreground flex-shrink-0 mt-0.5" />
        <p className="text-xs text-muted-foreground">
          {t("confluence.bind.depthExplanation")}
        </p>
      </div>
    </div>
  );
}

// ============================================================
// Main Component
// ============================================================

export default function BindSpacePage() {
  const navigate = useNavigate();
  const { connectionId } = useParams<{ connectionId: string }>();
  const queryClient = useQueryClient();
  const { t } = useTranslation();

  // Current step
  const [currentStep, setCurrentStep] = useState(0);

  // Form state
  const [selectedDatasetId, setSelectedDatasetId] = useState("");
  const [selectedSpaceKey, setSelectedSpaceKey] = useState("");
  const [selectedSpaceName, setSelectedSpaceName] = useState("");
  const [selectedPages, setSelectedPages] = useState<Map<string, SelectedPage>>(
    new Map()
  );
  const [syncEntireSpace, setSyncEntireSpace] = useState(true);
  const [maxDepth, setMaxDepth] = useState(10);
  const [includeAttachments, setIncludeAttachments] = useState(false);
  const [includeComments, setIncludeComments] = useState(false);

  // Queries
  const { data: connection, isLoading: loadingConnection } = useQuery({
    queryKey: ["confluence-connection", connectionId],
    queryFn: () => getConnection(connectionId!),
    enabled: !!connectionId,
  });

  const { data: datasets = [], isLoading: loadingDatasets } = useQuery({
    queryKey: ["kb-datasets"],
    queryFn: () => listDatasets(),
  });

  const {
    data: spacesResponse,
    isLoading: loadingSpaces,
    error: spacesError,
  } = useQuery({
    queryKey: ["confluence-spaces", connectionId],
    queryFn: () => discoverSpaces(connectionId!),
    enabled: !!connectionId,
  });

  const {
    data: pageTreeResponse,
    isLoading: loadingPageTree,
  } = useQuery({
    queryKey: ["confluence-page-tree", connectionId, selectedSpaceKey],
    queryFn: () => discoverSpacePages(connectionId!, selectedSpaceKey, 5),
    enabled: !!connectionId && !!selectedSpaceKey,
  });

  // Create binding mutation
  const createBindingMutation = useMutation({
    mutationFn: (data: ConfluenceBindingCreateRequest) =>
      createBinding(connectionId!, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["confluence-bindings"] });
      navigate("/confluence");
    },
  });

  // Convert spaces to combobox options
  const spaceOptions: ComboboxOption[] = useMemo(() => {
    if (!spacesResponse?.spaces) return [];
    return spacesResponse.spaces.map((space) => ({
      value: space.space_key,
      label: space.name,
      description: `${space.type === "personal" ? t("confluence.spaceType.personal") : t("confluence.spaceType.global")} • ${space.space_key}`,
      icon: <Folder className="h-4 w-4" />,
      space_id: space.space_id,
      type: space.type,
    }));
  }, [spacesResponse, t]);

  // Convert datasets to combobox options
  const datasetOptions: ComboboxOption[] = useMemo(() => {
    return datasets.map((ds) => ({
      value: ds.dataset_id,
      label: ds.name,
      description: ds.description || `${ds.statistics?.document_count || 0} documents`,
      icon: <Database className="h-4 w-4" />,
    }));
  }, [datasets]);

  // Handle space selection
  const handleSpaceSelect = (value: string, option: ComboboxOption | undefined) => {
    setSelectedSpaceKey(value);
    setSelectedSpaceName(option?.label || value);
    setSelectedPages(new Map());
    setSyncEntireSpace(true);
  };

  // Handle page toggle (single-select mode)
  const handlePageToggle = useCallback(
    (pageId: string, title: string, hasChildren: boolean) => {
      setSelectedPages((prev) => {
        // Single-select: toggle the clicked page
        if (prev.has(pageId)) {
          // Deselect if already selected - reset to sync entire space
          setSyncEntireSpace(true);
          return new Map();
        } else {
          // Select only this page (replace any existing selection)
          const newMap = new Map<string, SelectedPage>();
          newMap.set(pageId, { pageId, title, hasChildren });
          setSyncEntireSpace(false);
          return newMap;
        }
      });
    },
    []
  );

  // Clear page selection
  const handleClearSelection = () => {
    setSelectedPages(new Map());
    setSyncEntireSpace(true);
  };


  // Check if can proceed to next step
  const canProceed = () => {
    switch (currentStep) {
      case 0:
        return !!selectedDatasetId;
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
    if (!selectedDatasetId || !selectedSpaceKey) return;

    // Get the selected root page (single-select mode)
    const selectedPage = Array.from(selectedPages.values())[0];
    const rootPageId = syncEntireSpace ? undefined : selectedPage?.pageId;

    createBindingMutation.mutate({
      dataset_id: selectedDatasetId,
      space_key: selectedSpaceKey,
      root_page_id: rootPageId,
      max_depth: maxDepth,
      include_attachments: includeAttachments,
      include_comments: includeComments,
    });
  };

  const steps = [
    { label: t("confluence.bind.steps.dataset"), icon: <Database className="h-4 w-4" /> },
    { label: t("confluence.bind.steps.space"), icon: <Folder className="h-4 w-4" /> },
    { label: t("confluence.bind.steps.options"), icon: <Settings2 className="h-4 w-4" /> },
  ];

  if (loadingConnection) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-b from-background to-muted/20">
      {/* Header */}
      <div className="bg-card/80 backdrop-blur-sm border-b border-border/50 sticky top-0 z-20">
        <div className="max-w-4xl mx-auto px-6">
          <div className="flex items-center h-16 gap-4">
            <Button
              variant="ghost"
              size="icon"
              onClick={() => navigate("/confluence")}
              className="h-9 w-9"
            >
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-cyan-500 to-teal-500 flex items-center justify-center">
                <Link2 className="h-5 w-5 text-white" />
              </div>
              <div>
                <h1 className="text-lg font-semibold text-foreground">{t("confluence.bind.title")}</h1>
                <p className="text-xs text-muted-foreground">
                  {connection?.name || "Confluence Connection"}
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="max-w-4xl mx-auto px-6 py-8">
        <StepIndicator currentStep={currentStep} steps={steps} />

        {/* Step 0: Select Dataset */}
        {currentStep === 0 && (
          <Card className="p-6 border-border/60">
            <div className="flex items-center gap-3 mb-6">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-violet-500/10 to-purple-500/10 border border-violet-500/20 flex items-center justify-center">
                <Database className="h-5 w-5 text-violet-500" />
              </div>
              <div>
                <h2 className="font-semibold text-foreground">{t("confluence.bind.selectDataset")}</h2>
                <p className="text-sm text-muted-foreground">
                  {t("confluence.bind.selectDatasetDesc")}
                </p>
              </div>
            </div>

            <div className="space-y-4">
              <Label className="text-sm font-medium">{t("confluence.bind.knowledgeBase")}</Label>
              <Combobox
                options={datasetOptions}
                value={selectedDatasetId}
                onChange={(value) => setSelectedDatasetId(value)}
                placeholder={t("confluence.bind.searchDatasets")}
                searchPlaceholder={t("confluence.bind.searchDatasets")}
                emptyText={t("confluence.bind.noDatasets")}
                loading={loadingDatasets}
                renderOption={(option, isSelected) => (
                  <div className="flex items-center gap-3 w-full">
                    <div className="w-8 h-8 rounded-lg bg-violet-500/10 flex items-center justify-center flex-shrink-0">
                      <Database className="h-4 w-4 text-violet-500" />
                    </div>
                    <div className="flex-1 min-w-0">
                      <div className="font-medium truncate">{option.label}</div>
                      {option.description && (
                        <div className="text-xs text-muted-foreground truncate">
                          {option.description}
                        </div>
                      )}
                    </div>
                    {isSelected && (
                      <CheckCircle className="h-4 w-4 text-primary flex-shrink-0" />
                    )}
                  </div>
                )}
              />

              {datasets.length === 0 && !loadingDatasets && (
                <div className="p-4 bg-amber-50 dark:bg-amber-950/30 border border-amber-200 dark:border-amber-900 rounded-lg">
                  <p className="text-sm text-amber-700 dark:text-amber-400">
                    {t("confluence.bind.noDatasetsDesc")}
                  </p>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-2"
                    onClick={() => navigate("/knowledge/create")}
                  >
                    {t("confluence.bind.createDataset")}
                  </Button>
                </div>
              )}
            </div>
          </Card>
        )}

        {/* Step 1: Select Space & Pages */}
        {currentStep === 1 && (
          <Card className="p-6 border-border/60">
            <div className="flex items-center gap-3 mb-6">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-blue-500/10 to-cyan-500/10 border border-blue-500/20 flex items-center justify-center">
                <Folder className="h-5 w-5 text-blue-500" />
              </div>
              <div>
                <h2 className="font-semibold text-foreground">{t("confluence.bind.selectSpace")}</h2>
                <p className="text-sm text-muted-foreground">
                  {t("confluence.bind.selectSpaceDesc")}
                </p>
              </div>
            </div>

            {spacesError ? (
              <div className="p-4 bg-rose-50 dark:bg-rose-950/30 border border-rose-200 dark:border-rose-900 rounded-lg">
                <div className="flex items-center gap-2 text-rose-700 dark:text-rose-400">
                  <AlertCircle className="h-5 w-5" />
                  <span className="font-medium">{t("confluence.bind.failedToLoadSpaces")}</span>
                </div>
                <p className="text-sm text-rose-600 dark:text-rose-500 mt-1">
                  {spacesError instanceof Error ? spacesError.message : t("common.error")}
                </p>
              </div>
            ) : (
              <div className="space-y-6">
                {/* Space Selector */}
                <div className="space-y-2">
                  <Label className="text-sm font-medium flex items-center gap-2">
                    <Search className="h-4 w-4 text-muted-foreground" />
                    {t("confluence.bind.space")}
                  </Label>
                  <Combobox
                    options={spaceOptions}
                    value={selectedSpaceKey}
                    onChange={handleSpaceSelect}
                    placeholder={t("confluence.bind.selectSpacePlaceholder")}
                    searchPlaceholder={t("confluence.bind.searchSpaces")}
                    emptyText={t("confluence.bind.noDatasets")}
                    loading={loadingSpaces}
                    renderOption={(option, isSelected) => (
                      <div className="flex items-center gap-3 w-full">
                        <div
                          className={cn(
                            "w-8 h-8 rounded-lg flex items-center justify-center flex-shrink-0",
                            option.type === "personal"
                              ? "bg-amber-500/10"
                              : "bg-blue-500/10"
                          )}
                        >
                          <Folder
                            className={cn(
                              "h-4 w-4",
                              option.type === "personal"
                                ? "text-amber-500"
                                : "text-blue-500"
                            )}
                          />
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="font-medium truncate">{option.label}</div>
                          <div className="text-xs text-muted-foreground truncate">
                            {option.description}
                          </div>
                        </div>
                        {isSelected && (
                          <CheckCircle className="h-4 w-4 text-primary flex-shrink-0" />
                        )}
                      </div>
                    )}
                    renderValue={(option) => {
                      if (!option) return <span className="text-muted-foreground">{t("confluence.bind.selectSpacePlaceholder")}</span>;
                      return (
                        <div className="flex items-center gap-2">
                          <Folder className="h-4 w-4 text-blue-500" />
                          <span>{option.label}</span>
                          <Badge variant="outline" className="text-xs">
                            {option.value}
                          </Badge>
                        </div>
                      );
                    }}
                  />
                </div>

                {/* Page Tree with Multi-Select */}
                {selectedSpaceKey && (
                  <div className="space-y-3">
                    <div className="flex items-center justify-between">
                      <Label className="text-sm font-medium flex items-center gap-2">
                        <FolderTree className="h-4 w-4 text-muted-foreground" />
                        {t("confluence.bind.selectPages")}
                      </Label>
                      {selectedPages.size > 0 && (
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={handleClearSelection}
                          className="h-7 text-xs text-muted-foreground"
                        >
                          {t("confluence.bind.clearSelection")}
                        </Button>
                      )}
                    </div>

                    <p className="text-xs text-muted-foreground">
                      {t("confluence.bind.singlePageHint")}
                    </p>

                    {/* Sync entire space toggle */}
                    <div
                      className={cn(
                        "flex items-center gap-3 p-3 rounded-lg border cursor-pointer transition-all",
                        syncEntireSpace
                          ? "bg-primary/5 border-primary/30"
                          : "hover:bg-muted/50 border-border"
                      )}
                      onClick={() => {
                        setSyncEntireSpace(true);
                        setSelectedPages(new Map());
                      }}
                    >
                      {syncEntireSpace ? (
                        <CheckSquare className="h-5 w-5 text-primary" />
                      ) : (
                        <Square className="h-5 w-5 text-muted-foreground" />
                      )}
                      <Folder className="h-4 w-4 text-amber-500" />
                      <div className="flex-1">
                        <span className="text-sm font-medium">
                          {t("confluence.bind.entireSpace")}
                        </span>
                        <p className="text-xs text-muted-foreground">
                          {t("confluence.bind.entireSpaceDesc")}
                        </p>
                      </div>
                    </div>

                    {/* Page tree */}
                    <div className="border rounded-lg bg-background max-h-[350px] overflow-y-auto">
                      {loadingPageTree ? (
                        <div className="flex items-center justify-center py-8">
                          <Loader2 className="h-5 w-5 animate-spin text-primary" />
                        </div>
                      ) : pageTreeResponse?.root_pages &&
                        pageTreeResponse.root_pages.length > 0 ? (
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
                        <div className="py-8 text-center text-sm text-muted-foreground">
                          {t("confluence.bind.noPages")}
                        </div>
                      )}
                    </div>

                    {/* Selected pages summary */}
                    {selectedPages.size > 0 && (
                      <div className="flex flex-wrap gap-2 p-3 bg-primary/5 border border-primary/20 rounded-lg">
                        <span className="text-xs text-primary font-medium mr-1">
                          {t("confluence.bind.selectedCount", { count: selectedPages.size })}:
                        </span>
                        {Array.from(selectedPages.values())
                          .slice(0, 5)
                          .map((page) => (
                            <Badge
                              key={page.pageId}
                              variant="secondary"
                              className="text-xs gap-1"
                            >
                              {page.hasChildren ? (
                                <Folder className="h-3 w-3" />
                              ) : (
                                <FileText className="h-3 w-3" />
                              )}
                              {page.title}
                            </Badge>
                          ))}
                        {selectedPages.size > 5 && (
                          <Badge variant="outline" className="text-xs">
                            +{selectedPages.size - 5}
                          </Badge>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </Card>
        )}

        {/* Step 2: Options */}
        {currentStep === 2 && (
          <Card className="p-6 border-border/60">
            <div className="flex items-center gap-3 mb-6">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-teal-500/10 to-emerald-500/10 border border-teal-500/20 flex items-center justify-center">
                <Settings2 className="h-5 w-5 text-teal-500" />
              </div>
              <div>
                <h2 className="font-semibold text-foreground">{t("confluence.bind.syncOptions")}</h2>
                <p className="text-sm text-muted-foreground">
                  {t("confluence.bind.syncOptionsDesc")}
                </p>
              </div>
            </div>

            {/* Summary */}
            <div className="mb-6 p-4 bg-muted/30 rounded-lg border border-border/50">
              <div className="flex items-center gap-2 text-sm flex-wrap">
                <Database className="h-4 w-4 text-violet-500" />
                <span className="font-medium">
                  {datasets.find((d) => d.dataset_id === selectedDatasetId)?.name}
                </span>
                <ChevronRight className="h-4 w-4 text-muted-foreground" />
                <Folder className="h-4 w-4 text-blue-500" />
                <span className="font-medium">{selectedSpaceName}</span>
                {!syncEntireSpace && selectedPages.size > 0 && (
                  <>
                    <ChevronRight className="h-4 w-4 text-muted-foreground" />
                    <Badge variant="secondary" className="text-xs">
                      {selectedPages.size} {t("confluence.bind.pagesSelected")}
                    </Badge>
                  </>
                )}
              </div>
            </div>

            <div className="space-y-6">
              {/* Max Depth - Visual Selector */}
              <div className="space-y-3">
                <Label className="text-sm font-medium flex items-center gap-2">
                  <Layers className="h-4 w-4 text-muted-foreground" />
                  {t("confluence.bind.depthLimit")}
                </Label>
                <DepthSelector value={maxDepth} onChange={setMaxDepth} t={t} />
              </div>

              {/* Include Options */}
              <div className="space-y-4">
                <div className="flex items-center justify-between p-3 border rounded-lg">
                  <div className="space-y-0.5">
                    <Label className="text-sm font-medium">{t("confluence.bind.includeAttachments")}</Label>
                    <p className="text-xs text-muted-foreground">
                      {t("confluence.bind.includeAttachmentsDesc")}
                    </p>
                  </div>
                  <Switch
                    checked={includeAttachments}
                    onCheckedChange={setIncludeAttachments}
                  />
                </div>

                <div className="flex items-center justify-between p-3 border rounded-lg">
                  <div className="space-y-0.5">
                    <Label className="text-sm font-medium">{t("confluence.bind.includeComments")}</Label>
                    <p className="text-xs text-muted-foreground">
                      {t("confluence.bind.includeCommentsDesc")}
                    </p>
                  </div>
                  <Switch
                    checked={includeComments}
                    onCheckedChange={setIncludeComments}
                  />
                </div>
              </div>
            </div>
          </Card>
        )}

        {/* Navigation */}
        <div className="flex items-center justify-between mt-8">
          <Button
            variant="ghost"
            onClick={() => {
              if (currentStep === 0) {
                navigate("/confluence");
              } else {
                setCurrentStep((prev) => prev - 1);
              }
            }}
          >
            <ArrowLeft className="h-4 w-4 mr-2" />
            {currentStep === 0 ? t("common.cancel") : t("common.back")}
          </Button>

          <div className="flex items-center gap-3">
            {currentStep === 2 ? (
              <Button
                onClick={handleCreate}
                disabled={createBindingMutation.isPending}
                className="bg-gradient-to-r from-cyan-500 to-teal-500 hover:from-cyan-600 hover:to-teal-600 text-white border-0 min-w-[140px]"
              >
                {createBindingMutation.isPending ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    {t("confluence.bind.creating")}
                  </>
                ) : (
                  <>
                    <Link2 className="h-4 w-4 mr-2" />
                    {t("confluence.bind.createBinding")}
                  </>
                )}
              </Button>
            ) : (
              <Button
                onClick={() => setCurrentStep((prev) => prev + 1)}
                disabled={!canProceed()}
                className="bg-gradient-to-r from-blue-500 to-cyan-500 hover:from-blue-600 hover:to-cyan-600 text-white border-0"
              >
                {t("common.next")}
                <ChevronRight className="h-4 w-4 ml-1" />
              </Button>
            )}
          </div>
        </div>

        {/* Error display */}
        {createBindingMutation.isError && (
          <Card className="mt-4 p-4 border-rose-200 bg-rose-50 dark:bg-rose-950/30">
            <div className="flex items-center gap-3">
              <AlertCircle className="h-5 w-5 text-rose-600 flex-shrink-0" />
              <div>
                <p className="font-medium text-rose-800 dark:text-rose-400">
                  {t("confluence.bind.createFailed")}
                </p>
                <p className="text-sm text-rose-600 dark:text-rose-500">
                  {createBindingMutation.error instanceof Error
                    ? createBindingMutation.error.message
                    : t("common.error")}
                </p>
              </div>
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}
