export const streamStartMetrics = {
  starts: 0,
  awaitedSessionCreate: 0,
};

export function resetStreamStartMetrics(): void {
  streamStartMetrics.starts = 0;
  streamStartMetrics.awaitedSessionCreate = 0;
}

export function mintClientSessionId(
  randomUUID: () => string = () => crypto.randomUUID(),
): string {
  return randomUUID();
}

export function beginNewChatSession(
  activeSessionId: string | undefined,
  mint: () => string = mintClientSessionId,
): { sessionId: string; isNew: boolean } {
  if (activeSessionId) {
    return { sessionId: activeSessionId, isNew: false };
  }
  return { sessionId: mint(), isNew: true };
}

export function persistNewChatSession(
  createSession: (body: {
    session_id: string;
    service_id?: string;
    metadata?: Record<string, unknown>;
    config?: unknown;
  }) => Promise<{ session_id: string }>,
  input: { sessionId: string; title: string; config?: unknown },
): Promise<{ session_id: string }> {
  return createSession({
    session_id: input.sessionId,
    service_id: "__builtin_assistant__",
    metadata: { title: input.title },
    config: input.config,
  });
}

export function startChatWithoutAwaitingSessionCreate<TStream>(args: {
  sessionId: string;
  isNew: boolean;
  openStream: (sessionId: string) => TStream;
  persistSession?: (sessionId: string) => Promise<unknown>;
}): TStream {
  streamStartMetrics.starts += 1;
  const stream = args.openStream(args.sessionId);
  if (args.isNew && args.persistSession) {
    void args.persistSession(args.sessionId);
  }
  return stream;
}
