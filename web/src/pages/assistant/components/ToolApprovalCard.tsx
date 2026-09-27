import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { getAgentRuntimeApproval, type AgentApprovalPreview } from "@/api/agentThreads";
import { capabilityDisplayName } from "@/pages/agents/agentCatalogPresentation";
import type { ToolTimelineItem } from "../types";

export function ToolApprovalCard({
  tool,
  runtimeThreadId,
  sourceAccessRevoked = false,
  onApprove,
  onReject,
}: {
  tool: ToolTimelineItem;
  runtimeThreadId?: string;
  sourceAccessRevoked?: boolean;
  onApprove: () => void | Promise<void>;
  onReject: () => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const [preview, setPreview] = useState<AgentApprovalPreview | null>(null);
  const [approvalStatus, setApprovalStatus] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [expiresAtMs, setExpiresAtMs] = useState<number | null>(null);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const pendingRef = useRef(false);
  const displayName = capabilityDisplayName(tool.name);

  const refresh = useCallback(async () => {
    if (!runtimeThreadId || !tool.approvalId) {
      setError(t("assistant.activity.approvalDetailsUnavailable", "Action details cannot be verified."));
      return null;
    }
    try {
      const value = await getAgentRuntimeApproval(runtimeThreadId, tool.approvalId);
      setApprovalStatus(value.approval.status);
      setPreview(value.preview);
      const checkedAt = Date.now();
      setNowMs(checkedAt);
      setExpiresAtMs(typeof value.preview.expires_in_seconds === "number"
        ? checkedAt + value.preview.expires_in_seconds * 1000 : null);
      setError("");
      return value;
    } catch {
      setError(t("assistant.activity.approvalDetailsUnavailable", "Action details cannot be verified."));
      return null;
    }
  }, [runtimeThreadId, tool.approvalId, t]);

  useEffect(() => {
    if (sourceAccessRevoked) setPreview(null);
    void refresh();
  }, [refresh, sourceAccessRevoked]);

  useEffect(() => {
    if (approvalStatus !== "pending" || expiresAtMs === null) return;
    const timer = window.setInterval(() => {
      const now = Date.now();
      setNowMs(now);
      if (now >= expiresAtMs) {
        window.clearInterval(timer);
        void refresh();
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [approvalStatus, expiresAtMs, refresh]);
  const expiresInSeconds = expiresAtMs === null ? null : Math.max(0, Math.ceil((expiresAtMs - nowMs) / 1000));

  const decide = async (approved: boolean) => {
    if (pendingRef.current || approvalStatus !== "pending") return;
    if (approved && (sourceAccessRevoked || !preview?.can_approve)) return;
    pendingRef.current = true;
    setBusy(true);
    try {
      const latest = await refresh();
      if (!latest || latest.approval.status !== "pending") return;
      if (approved) {
        if (!latest.preview.can_approve || latest.preview.arguments_hash !== preview?.arguments_hash) {
          setError(t("assistant.activity.approvalChanged", "Action details changed or expired. Review the current request before approving."));
          return;
        }
        await onApprove();
      } else {
        await onReject();
      }
      await refresh();
    } catch {
      await refresh();
      setError(t("assistant.activity.approvalDecisionFailed", "Decision could not be recorded. Check the current request before trying again."));
    } finally {
      pendingRef.current = false;
      setBusy(false);
    }
  };

  return (
    <div role="alertdialog" aria-labelledby={`approval-${tool.id}-title`} className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-4">
      <p id={`approval-${tool.id}-title`} className="text-sm font-semibold">
        {t("assistant.activity.approvalRequired", "Approval required")}: {displayName}
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        {t("assistant.activity.approvalScope", "Approval applies to this action only. A changed or expired request needs a new decision.")}
      </p>
      {preview?.can_approve && !sourceAccessRevoked ? (
        <div className="mt-2 space-y-1 text-xs">
          <p><strong>{t("assistant.activity.approvalEffect", "Effect")}:</strong> {preview.effect}</p>
          <p><strong>{t("assistant.activity.approvalTarget", "Target")}:</strong> {preview.target}</p>
          <p><strong>{t("assistant.activity.approvalExpiry", "Expires in")}:</strong> {expiresInSeconds}s</p>
          <div className="max-h-52 space-y-1 overflow-y-auto rounded border border-amber-500/20 bg-background/50 p-2">
            {preview.parameters?.map((parameter) => (
              <p key={parameter.name} className="break-all whitespace-pre-wrap"><strong>{parameter.name}:</strong> {parameter.value}</p>
            ))}
          </div>
        </div>
      ) : (
        <p className="mt-2 text-xs text-amber-800 dark:text-amber-200">
          {sourceAccessRevoked ? t("assistant.sourceNeedsNewConversation") : preview?.reason || error || t("assistant.activity.approvalLoading", "Verifying this action…")}
        </p>
      )}
      {error && preview?.can_approve && <p role="alert" className="mt-2 text-xs text-red-600">{error}</p>}
      <div className="mt-3 flex gap-2">
        <button type="button" disabled={busy || approvalStatus !== "pending" || sourceAccessRevoked || !preview?.can_approve || expiresInSeconds === 0} onClick={() => void decide(true)} className="rounded-md bg-[hsl(var(--assistant-accent))] px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50">
          {t("common.approve", "Approve")}
        </button>
        <button type="button" disabled={busy || approvalStatus !== "pending"} onClick={() => void decide(false)} className="rounded-md border border-[hsl(var(--assistant-border))] px-3 py-1.5 text-xs disabled:opacity-50">
          {t("common.reject", "Reject")}
        </button>
      </div>
    </div>
  );
}

export default ToolApprovalCard;
