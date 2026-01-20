/**
 * ConfluenceSyncTab - Main Confluence sync management tab
 */

import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Plus, Server, Loader2, AlertCircle } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ConnectionCard } from "./ConnectionCard";
import { BindingTable } from "./BindingTable";
import { SchedulerStatus } from "./SchedulerStatus";
import {
  useConnections,
  useBindings,
  useSchedulerStatus,
  useConnectionStats,
} from "../hooks/useConfluenceSync";

export function ConfluenceSyncTab() {
  const { t } = useTranslation();
  const [selectedConnectionId, setSelectedConnectionId] = useState<
    string | null
  >(null);

  // Data fetching
  const {
    data: connections,
    isLoading: connectionsLoading,
    error: connectionsError,
  } = useConnections();
  const {
    data: bindings,
    isLoading: bindingsLoading,
  } = useBindings(selectedConnectionId || undefined);
  const {
    data: schedulerStatus,
    isLoading: schedulerLoading,
    refetch: refetchScheduler,
  } = useSchedulerStatus();

  // Connection stats
  const connectionStats = useConnectionStats(connections);

  // Auto-select first connection
  if (
    connections &&
    connections.length > 0 &&
    !selectedConnectionId &&
    !connectionsLoading
  ) {
    setSelectedConnectionId(connections[0].connection_id);
  }

  // Error state
  if (connectionsError) {
    return (
      <div className="flex flex-col items-center justify-center py-16">
        <AlertCircle className="h-12 w-12 text-destructive mb-4" />
        <p className="text-sm font-medium text-destructive">
          {t("tasks.confluence.loadError")}
        </p>
        <p className="text-xs text-muted-foreground mt-1">
          {connectionsError instanceof Error
            ? connectionsError.message
            : t("tasks.confluence.loadErrorDesc")}
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Scheduler Status Bar */}
      <SchedulerStatus
        status={schedulerStatus}
        isLoading={schedulerLoading}
        onRefresh={() => refetchScheduler()}
      />

      {/* Main Content */}
      <div className="grid grid-cols-12 gap-4">
        {/* Left: Connection Pool */}
        <div className="col-span-4">
          <Card className="h-[calc(100vh-280px)]">
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base flex items-center gap-2">
                  <Server className="h-4 w-4" />
                  {t("tasks.confluence.connectionPool")}
                </CardTitle>
                {/* TODO: Add connection dialog */}
              </div>
            </CardHeader>
            <CardContent className="p-0">
              {connectionsLoading ? (
                <div className="flex items-center justify-center py-12">
                  <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                </div>
              ) : connections && connections.length > 0 ? (
                <ScrollArea className="h-[calc(100vh-360px)] px-4">
                  <div className="space-y-3 pb-4">
                    {connections.map((conn) => {
                      const stats = connectionStats[conn.connection_id] || {
                        bindingCount: 0,
                        lastSyncAt: null,
                      };
                      return (
                        <ConnectionCard
                          key={conn.connection_id}
                          connection={conn}
                          bindingCount={stats.bindingCount}
                          lastSyncAt={stats.lastSyncAt}
                          isSelected={selectedConnectionId === conn.connection_id}
                          onSelect={() =>
                            setSelectedConnectionId(conn.connection_id)
                          }
                        />
                      );
                    })}
                  </div>
                </ScrollArea>
              ) : (
                <div className="flex flex-col items-center justify-center py-12 text-center px-4">
                  <Server className="h-12 w-12 text-muted-foreground/40 mb-4" />
                  <p className="text-sm font-medium text-muted-foreground">
                    {t("tasks.confluence.noConnections")}
                  </p>
                  <p className="text-xs text-muted-foreground/70 mt-1 mb-4">
                    {t("tasks.confluence.noConnectionsDesc")}
                  </p>
                  <Button size="sm" variant="outline">
                    <Plus className="h-4 w-4 mr-1" />
                    {t("tasks.confluence.addConnection")}
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Right: Binding Details */}
        <div className="col-span-8">
          <Card className="h-[calc(100vh-280px)]">
            <CardHeader className="pb-3">
              <CardTitle className="text-base">
                {selectedConnectionId
                  ? t("tasks.confluence.bindingDetails")
                  : t("tasks.confluence.selectConnection")}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {selectedConnectionId ? (
                <ScrollArea className="h-[calc(100vh-380px)]">
                  <BindingTable
                    bindings={bindings || []}
                    isLoading={bindingsLoading}
                  />
                </ScrollArea>
              ) : (
                <div className="flex flex-col items-center justify-center py-16 text-center">
                  <Server className="h-12 w-12 text-muted-foreground/40 mb-4" />
                  <p className="text-sm text-muted-foreground">
                    {t("tasks.confluence.selectConnectionHint")}
                  </p>
                </div>
              )}
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
