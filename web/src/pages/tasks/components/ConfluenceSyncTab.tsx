/**
 * ConfluenceSyncTab - Main Confluence sync management tab
 */

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
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
  const navigate = useNavigate();
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

  // Keep selection aligned with the loaded connection set.
  useEffect(() => {
    if (connectionsLoading) return;
    const nextSelection =
      connections?.some((connection) => connection.connection_id === selectedConnectionId)
        ? selectedConnectionId
        : connections?.[0]?.connection_id || null;
    if (nextSelection === selectedConnectionId) return;
    setSelectedConnectionId(nextSelection);
  }, [connections, connectionsLoading, selectedConnectionId]);

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
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-12">
        {/* Left: Connection Pool */}
        <div className={connections && connections.length > 0 ? "xl:col-span-4" : "xl:col-span-12"}>
          <Card className={connections && connections.length > 0 ? "xl:h-[calc(100vh-280px)]" : "mx-auto w-full max-w-2xl"}>
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between">
                <CardTitle className="text-base flex items-center gap-2">
                  <Server className="h-4 w-4" />
                  {t("tasks.confluence.connectionPool")}
                </CardTitle>
                {connections && connections.length > 0 && (
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-10 px-2 sm:h-8"
                    onClick={() => navigate("/confluence/connections/new")}
                    aria-label={t("tasks.confluence.addConnection")}
                  >
                    <Plus className="mr-1 h-4 w-4" />
                    <span className="hidden sm:inline">{t("tasks.confluence.addConnection")}</span>
                  </Button>
                )}
              </div>
            </CardHeader>
            <CardContent className="p-0">
              {connectionsLoading ? (
                <div className="flex items-center justify-center py-12">
                  <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
                </div>
              ) : connections && connections.length > 0 ? (
                <ScrollArea className="max-h-[420px] px-4 xl:h-[calc(100vh-360px)] xl:max-h-none">
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
                  <Button
                    size="sm"
                    variant="primary"
                    onClick={() => navigate("/confluence/connections/new")}
                  >
                    <Plus className="h-4 w-4 mr-1" />
                    {t("tasks.confluence.addConnection")}
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Right: Binding Details */}
        <div className={connections && connections.length > 0 ? "xl:col-span-8" : "hidden"}>
          <Card className="xl:h-[calc(100vh-280px)]">
            <CardHeader className="pb-3">
              <CardTitle className="text-base">
                {selectedConnectionId
                  ? t("tasks.confluence.bindingDetails")
                  : t("tasks.confluence.selectConnection")}
              </CardTitle>
            </CardHeader>
            <CardContent>
              {selectedConnectionId ? (
                <ScrollArea className="max-h-[520px] xl:h-[calc(100vh-380px)] xl:max-h-none">
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
