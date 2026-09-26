import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { getAssistantSessionTools, type AssistantSessionTools } from "@/api/assistantTools";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";

interface ToolsDialogProps {
  sessionId: string;
  open: boolean;
  onClose: () => void;
}

export function ToolsDialog({ sessionId, open, onClose }: ToolsDialogProps) {
  const { t } = useTranslation();
  const [data, setData] = useState<AssistantSessionTools | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    let disposed = false;
    setData(null);
    setError("");
    void getAssistantSessionTools(sessionId).then((value) => {
      if (!disposed) setData(value);
    }).catch(() => {
      if (!disposed) setError(t("assistant.toolsUnavailable", "Tool status is unavailable. Check the connection or worker configuration."));
    });
    return () => { disposed = true; };
  }, [open, sessionId, t]);

  const nextNames = new Set(data?.next_turn_estimate.tools.map((tool) => tool.name) || []);
  const pinnedNames = new Set(data?.last_run_pinned?.tools.map((tool) => tool.name) || []);

  return (
    <Dialog open={open} onOpenChange={(value) => { if (!value) onClose(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{t("assistant.toolsTitle", "Assistant tools")}</DialogTitle>
          <DialogDescription>{t("assistant.toolsScope", "The platform catalog, your tenant access, and this conversation's resolved tools are different sets. A new run pins its own set when it starts.")}</DialogDescription>
        </DialogHeader>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        {!data ? <p role="status" className="text-sm">{t("common.loading", "Loading…")}</p> : (
          <div className="space-y-3">
            <p className="text-xs text-muted-foreground">
              {data.next_turn_estimate.status === "unavailable"
                ? t("assistant.toolsEstimateUnavailable", "Current worker/configuration could not be checked; catalog entries are not confirmed callable.")
                : t("assistant.toolsEstimate", "Callable estimate if a turn started now; checked again at admission.")}
            </p>
            {data.last_run_pinned && (
              <p className="text-xs text-muted-foreground">
                {t("assistant.toolsPinned", "Last run {{run}} ({{status}}) pinned {{count}} tools.", {
                  run: data.last_run_pinned.run_id.slice(0, 8), status: data.last_run_pinned.status,
                  count: data.last_run_pinned.tools.length,
                })}
              </p>
            )}
            <div className="max-h-[55dvh] space-y-2 overflow-y-auto">
              {data.platform_catalog.map((tool) => {
                const canUseNow = nextNames.has(tool.name);
                const label = !tool.tenant_visible
                  ? t("assistant.toolNotAuthorized", "No tenant permission")
                  : data.next_turn_estimate.status === "unavailable"
                    ? t("assistant.toolUnknown", "Availability unknown")
                    : canUseNow
                      ? t("assistant.toolAvailableNow", "Available for a new run now")
                      : t("assistant.toolNotEnabled", "Not in the current resolved set");
                return (
                  <div key={tool.name} className="rounded border p-2 text-sm">
                    <div className="flex items-start justify-between gap-2">
                      <strong className="break-all">{tool.name}</strong>
                      <span className="shrink-0 text-xs text-muted-foreground">{label}</span>
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">{tool.description}</p>
                    {pinnedNames.has(tool.name) && <p className="mt-1 text-xs">{t("assistant.toolPinnedLastRun", "Included in the last run")}</p>}
                    {tool.device_required && <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">{t("assistant.toolNeedsDevice", "Choose an online device_id before use.")}</p>}
                    {tool.required_inputs.length > 0 && <p className="mt-1 text-xs text-muted-foreground">{t("assistant.toolRequiredInputs", "Required inputs")}: {tool.required_inputs.join(", ")}</p>}
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
