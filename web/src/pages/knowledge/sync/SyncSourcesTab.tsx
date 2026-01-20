/**
 * Sync Sources Tab Component
 *
 * Main tab for managing sync sources in dataset detail page.
 * Shows Confluence bindings and allows adding new ones.
 */

import { useState, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Plus,
  Cloud,
  RefreshCcw,
  Loader2,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

import { listBindings, listConnections } from "@/api/confluence";

import { SyncOverviewCards } from "./SyncOverviewCards";
import { ConfluenceBindingCard } from "./ConfluenceBindingCard";
import { AddConfluenceBindingDialog } from "./AddConfluenceBindingDialog";
import { BindingPagesPanel } from "./BindingPagesPanel";

interface SyncSourcesTabProps {
  datasetId: string;
}

export function SyncSourcesTab({ datasetId }: SyncSourcesTabProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const selectedBindingId = searchParams.get("binding");
  const [showAddDialog, setShowAddDialog] = useState(false);

  // Fetch all bindings for this dataset
  const {
    data: bindings = [],
    isLoading: loadingBindings,
    refetch: refetchBindings,
  } = useQuery({
    queryKey: ["kb-confluence-bindings", datasetId],
    queryFn: () => listBindings({ dataset_id: datasetId }),
    enabled: !!datasetId,
  });

  // Fetch all connections (for displaying connection names)
  const { data: connections = [] } = useQuery({
    queryKey: ["confluence-connections"],
    queryFn: () => listConnections(),
    staleTime: 30000, // Consider data fresh for 30s
  });

  // Create a map for quick connection lookup
  const connectionMap = useMemo(() => {
    return new Map(connections.map((c) => [c.connection_id, c]));
  }, [connections]);

  // Handle viewing pages of a binding
  const handleViewPages = (bindingId: string) => {
    setSearchParams({ tab: "sync", binding: bindingId });
  };

  // Handle going back from pages panel
  const handleBackFromPages = () => {
    setSearchParams({ tab: "sync" });
  };

  // If a binding is selected, show its pages panel
  if (selectedBindingId) {
    const selectedBinding = bindings.find(
      (b) => b.binding_id === selectedBindingId
    );

    // Handle case where binding was deleted or doesn't exist
    if (!selectedBinding && !loadingBindings) {
      // Clear invalid binding ID from URL
      setSearchParams({ tab: "sync" });
      return null;
    }

    return (
      <BindingPagesPanel
        bindingId={selectedBindingId}
        binding={selectedBinding}
        connection={selectedBinding ? connectionMap.get(selectedBinding.connection_id) : undefined}
        datasetId={datasetId}
        onBack={handleBackFromPages}
      />
    );
  }

  // Loading state
  if (loadingBindings) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Overview Cards */}
      <SyncOverviewCards bindings={bindings} />

      {/* Confluence Sync Section */}
      <Card className="p-6">
        <div className="flex items-center justify-between mb-6">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-blue-500/10 to-cyan-500/10 border border-blue-500/20 flex items-center justify-center">
              <Cloud className="h-5 w-5 text-blue-500" />
            </div>
            <div>
              <h3 className="font-semibold">Confluence 同步</h3>
              <p className="text-sm text-muted-foreground">
                将 Confluence 空间中的页面同步到此知识库
              </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => refetchBindings()}
            >
              <RefreshCcw className="h-4 w-4 mr-1.5" />
              刷新
            </Button>
            <Button onClick={() => setShowAddDialog(true)}>
              <Plus className="h-4 w-4 mr-1.5" />
              添加绑定
            </Button>
          </div>
        </div>

        {bindings.length === 0 ? (
          <EmptySyncState onAdd={() => setShowAddDialog(true)} />
        ) : (
          <div className="space-y-3">
            {bindings.map((binding) => (
              <ConfluenceBindingCard
                key={binding.binding_id}
                binding={binding}
                connection={connectionMap.get(binding.connection_id)}
                datasetId={datasetId}
                onViewPages={() => handleViewPages(binding.binding_id)}
              />
            ))}
          </div>
        )}
      </Card>

      {/* Future: Other sync sources can be added here */}
      {/* <Card className="p-6 opacity-50">
        <div className="flex items-center gap-3">
          <Database className="h-5 w-5 text-muted-foreground" />
          <div>
            <h3 className="font-semibold text-muted-foreground">更多数据源</h3>
            <p className="text-sm text-muted-foreground">
              Notion、Google Drive 等数据源即将支持
            </p>
          </div>
        </div>
      </Card> */}

      {/* Add Binding Dialog */}
      <AddConfluenceBindingDialog
        datasetId={datasetId}
        open={showAddDialog}
        onOpenChange={setShowAddDialog}
        onCreated={() => {
          refetchBindings();
        }}
      />
    </div>
  );
}

// Empty state component
function EmptySyncState({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="text-center py-12">
      <div className="w-16 h-16 mx-auto mb-4 rounded-2xl bg-gradient-to-br from-blue-500/10 to-cyan-500/10 border border-blue-500/20 flex items-center justify-center">
        <Cloud className="h-8 w-8 text-blue-500" />
      </div>
      <h3 className="font-semibold text-lg mb-2">还没有 Confluence 同步</h3>
      <p className="text-muted-foreground mb-6 max-w-md mx-auto">
        将 Confluence 空间绑定到此知识库，自动同步页面内容并保持更新
      </p>
      <Button onClick={onAdd}>
        <Plus className="h-4 w-4 mr-1.5" />
        添加 Confluence 绑定
      </Button>
    </div>
  );
}
