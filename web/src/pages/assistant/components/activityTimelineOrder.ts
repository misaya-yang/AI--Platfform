import type { TimelineStepData } from "./TimelineStep";

/** Keep execution rows fixed above the one independently-streaming reasoning row. */
export function appendReasoningAfterActivity(
  activitySteps: TimelineStepData[],
  reasoningStep: TimelineStepData | undefined,
): TimelineStepData[] {
  if (reasoningStep) activitySteps.push(reasoningStep);
  return activitySteps;
}
