import { useTranslation } from "react-i18next";

import { capabilityDisplayName } from "@/pages/agents/agentCatalogPresentation";
import type { ToolTimelineItem } from "../types";

export function ToolApprovalCard({
  tool,
  onApprove,
  onReject,
}: {
  tool: ToolTimelineItem;
  onApprove: () => void;
  onReject: () => void;
}) {
  const { t } = useTranslation();
  const displayName = capabilityDisplayName(tool.name);

  return (
    <div
      role="alertdialog"
      aria-labelledby={`approval-${tool.id}-title`}
      className="rounded-2xl border border-amber-500/40 bg-amber-500/10 p-4"
    >
      <p
        id={`approval-${tool.id}-title`}
        className="text-[14px] font-semibold text-[hsl(var(--assistant-text-primary))]"
      >
        {t("assistant.activity.approvalRequired", {
          defaultValue: "Approval required",
        })}
      </p>
      <p className="mt-1 text-[13px] text-[hsl(var(--assistant-text-secondary))]">
        {t("assistant.activity.approvalPrompt", {
          defaultValue: "The assistant wants to run {{tool}}. Approve to continue.",
          tool: displayName,
        })}
      </p>
      <p className="mt-1 font-mono text-[12px] text-[hsl(var(--assistant-text-tertiary))]">
        {displayName}
        {displayName !== tool.name ? ` · ${tool.name}` : ""}
      </p>
      {tool.summary ? (
        <p className="mt-2 whitespace-pre-wrap break-words text-[12px] text-[hsl(var(--assistant-text-secondary))]">
          {tool.summary}
        </p>
      ) : null}
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          className="rounded-md bg-[hsl(var(--assistant-accent))] px-3 py-1.5 text-[12px] font-medium text-white"
          onClick={onApprove}
        >
          {t("common.approve", { defaultValue: "Approve" })}
        </button>
        <button
          type="button"
          className="rounded-md border border-[hsl(var(--assistant-border))] px-3 py-1.5 text-[12px]"
          onClick={onReject}
        >
          {t("common.reject", { defaultValue: "Reject" })}
        </button>
      </div>
    </div>
  );
}

export default ToolApprovalCard;
