import type { ChatTurnState } from "../../features/chat/stream/types.ts";

export function restoredMessageStatus(summaryStatus: unknown): ChatTurnState {
  if (summaryStatus === "cancelled") return "cancelled";
  if (summaryStatus === "failed") return "failed";
  if (summaryStatus === "running" || summaryStatus === "blocked") return "streaming";
  return "completed";
}
