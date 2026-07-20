import { api } from "@/lib/api";
import { sseFetch } from "@/lib/sse";
import { useAuthStore } from "@/store/useAuthStore";
import type { AgentRuntimeSession, AgentStreamEvent } from "@/types/agents";

export type PublicAgentChannel = "hosted" | "embed";

export interface PublicAgentConfig {
  public_id: string;
  publication_id: string;
  channel: PublicAgentChannel;
  auth_mode: "private" | "tenant" | "public" | "token";
  name: string;
  description: string;
  identity: {
    icon_url?: string;
    theme_color?: string;
    welcome_message?: string;
    suggested_prompts?: string[];
  };
  attachments: boolean;
  request_id: string;
}

export interface PublicAgentAttachment {
  artifact_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  expires_at: string;
  request_id: string;
}

function authHeaders(embedToken?: string): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = useAuthStore.getState().token;
  if (token) headers.Authorization = `Bearer ${token}`;
  if (embedToken) headers["X-Agent-Embed-Token"] = embedToken;
  return headers;
}

export async function getPublicAgent(
  publicId: string,
  channel: PublicAgentChannel = "hosted",
  embedToken?: string,
): Promise<PublicAgentConfig> {
  const { data } = await api.get<PublicAgentConfig>(`/api/v1/public/agents/${publicId}`, {
    params: { channel },
    headers: embedToken ? { "X-Agent-Embed-Token": embedToken } : undefined,
  });
  return data;
}

export async function createPublicAgentSession(
  publicId: string,
  channel: PublicAgentChannel = "hosted",
  embedToken?: string,
): Promise<AgentRuntimeSession> {
  const { data } = await api.post<AgentRuntimeSession>(
    `/api/v1/public/agents/${publicId}/sessions`,
    { channel },
    { headers: embedToken ? { "X-Agent-Embed-Token": embedToken } : undefined },
  );
  return data;
}

export async function uploadPublicAgentAttachment(input: {
  publicId: string;
  file: File;
  channel?: PublicAgentChannel;
  embedToken?: string;
}): Promise<PublicAgentAttachment> {
  const form = new FormData();
  form.append("file", input.file);
  const { data } = await api.post<PublicAgentAttachment>(
    `/api/v1/public/agents/${input.publicId}/attachments`,
    form,
    {
      params: { channel: input.channel ?? "hosted" },
      headers: input.embedToken ? { "X-Agent-Embed-Token": input.embedToken } : undefined,
    },
  );
  return data;
}

export async function* streamPublicAgent(input: {
  publicId: string;
  channel?: PublicAgentChannel;
  embedToken?: string;
  sessionId: string;
  message: string;
  attachments?: Array<Pick<PublicAgentAttachment, "artifact_id" | "filename" | "mime_type">>;
  signal?: AbortSignal;
}): AsyncGenerator<AgentStreamEvent, void, void> {
  const channel = input.channel ?? "hosted";
  for await (const event of sseFetch<AgentStreamEvent>(
    `/api/v1/public/agents/${input.publicId}/chat/stream`,
    {
      method: "POST",
      headers: authHeaders(input.embedToken),
      body: JSON.stringify({
        message: input.message,
        session_id: input.sessionId,
        // The server's AgentRuntimeAttachment schema is closed (extra="forbid")
        // and only accepts artifact_id/filename/mime_type. Callers pass the full
        // upload-response objects (which also carry size_bytes/expires_at/
        // request_id), so project to the wire shape here or every chat turn with
        // an attachment fails 422 before the handler runs.
        attachments: (input.attachments ?? []).map(
          ({ artifact_id, filename, mime_type }) => ({ artifact_id, filename, mime_type }),
        ),
        channel,
      }),
      signal: input.signal,
    },
  )) {
    yield event;
  }
}

export async function submitPublicAgentFeedback(input: {
  publicId: string;
  channel?: PublicAgentChannel;
  embedToken?: string;
  sessionId: string;
  rating: -1 | 1;
  comment?: string;
}): Promise<void> {
  await api.post(
    `/api/v1/public/agents/${input.publicId}/feedback`,
    {
      session_id: input.sessionId,
      rating: input.rating,
      comment: input.comment ?? "",
      channel: input.channel ?? "hosted",
    },
    { headers: input.embedToken ? { "X-Agent-Embed-Token": input.embedToken } : undefined },
  );
}
