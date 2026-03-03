type ChatSurface = "assistant" | "playground";
type StreamOutcome = "completed" | "cancelled" | "failed";

interface TelemetryEnvelope {
  event: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

interface StreamTrace {
  id: string;
  surface: ChatSurface;
  startedAtMs: number;
  firstTokenMs?: number;
  attributes?: Record<string, unknown>;
}

declare global {
  interface Window {
    __AI_GATEWAY_PERF__?: {
      interactionDurations: number[];
      getInteractionP75: () => number;
      getMaxInteraction: () => number;
    };
  }
}

const PERF_SAMPLE_CAP = 500;
const DEFAULT_INP_THRESHOLD = 200;

let inpObserverInitialized = false;
const interactionDurations: number[] = [];

function percentile(values: number[], p: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const rank = (p / 100) * (sorted.length - 1);
  const lower = Math.floor(rank);
  const upper = Math.ceil(rank);
  if (lower === upper) return sorted[lower];
  const weight = rank - lower;
  return sorted[lower] * (1 - weight) + sorted[upper] * weight;
}

function pushInteractionDuration(duration: number) {
  interactionDurations.push(duration);
  if (interactionDurations.length > PERF_SAMPLE_CAP) {
    interactionDurations.splice(0, interactionDurations.length - PERF_SAMPLE_CAP);
  }
}

function generateId(prefix: string): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}-${Date.now()}`;
}

function emitTelemetry(event: string, payload: Record<string, unknown>) {
  const envelope: TelemetryEnvelope = {
    event,
    timestamp: new Date().toISOString(),
    payload,
  };

  window.dispatchEvent(
    new CustomEvent<TelemetryEnvelope>("ai-gateway:telemetry", {
      detail: envelope,
    })
  );

  const endpoint = import.meta.env.VITE_TELEMETRY_ENDPOINT;
  if (endpoint && typeof navigator.sendBeacon === "function") {
    try {
      navigator.sendBeacon(endpoint, JSON.stringify(envelope));
    } catch {
      // Ignore transport issues in UI path
    }
  }

  if (import.meta.env.DEV) {
    // Keep this visible while tuning interaction budgets in dev.
    console.debug("[telemetry]", envelope);
  }
}

export function initInteractionTelemetry() {
  if (inpObserverInitialized || typeof window === "undefined") return;
  inpObserverInitialized = true;

  window.__AI_GATEWAY_PERF__ = {
    interactionDurations,
    getInteractionP75: () => percentile(interactionDurations, 75),
    getMaxInteraction: () =>
      interactionDurations.length ? Math.max(...interactionDurations) : 0,
  };

  if (
    typeof PerformanceObserver === "undefined" ||
    !PerformanceObserver.supportedEntryTypes?.includes("event")
  ) {
    return;
  }

  const observer = new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      const eventEntry = entry as PerformanceEventTiming;
      if (!eventEntry.interactionId || eventEntry.duration <= 0) continue;
      pushInteractionDuration(eventEntry.duration);
      if (eventEntry.duration > DEFAULT_INP_THRESHOLD) {
        emitTelemetry("chat.performance.slow_interaction", {
          durationMs: Math.round(eventEntry.duration),
          thresholdMs: DEFAULT_INP_THRESHOLD,
          name: eventEntry.name,
        });
      }
    }
  });

  observer.observe({
    type: "event",
    buffered: true,
    durationThreshold: 16,
  });
}

export function trackChatShortcut(
  surface: ChatSurface,
  shortcut: string,
  action: string
) {
  emitTelemetry("chat.shortcut.triggered", {
    surface,
    shortcut,
    action,
  });
}

export function startChatStreamTrace(
  surface: ChatSurface,
  attributes?: Record<string, unknown>
): StreamTrace {
  const trace: StreamTrace = {
    id: generateId("stream-trace"),
    surface,
    startedAtMs: performance.now(),
    attributes,
  };
  emitTelemetry("chat.stream.started", {
    traceId: trace.id,
    surface,
    ...attributes,
  });
  return trace;
}

export function markChatStreamFirstToken(trace: StreamTrace, firstTokenMs: number) {
  if (trace.firstTokenMs != null) return;
  trace.firstTokenMs = firstTokenMs;
  emitTelemetry("chat.stream.first_token", {
    traceId: trace.id,
    surface: trace.surface,
    firstTokenMs,
    ...(trace.attributes || {}),
  });
}

export function finishChatStreamTrace(
  trace: StreamTrace,
  outcome: StreamOutcome,
  payload?: Record<string, unknown>
) {
  const durationMs = Math.round(performance.now() - trace.startedAtMs);
  emitTelemetry("chat.stream.finished", {
    traceId: trace.id,
    surface: trace.surface,
    outcome,
    durationMs,
    firstTokenMs: trace.firstTokenMs,
    ...(trace.attributes || {}),
    ...(payload || {}),
  });
}
