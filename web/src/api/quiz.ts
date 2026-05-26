/**
 * Quiz API client — generate, fetch, submit, and list quizzes.
 */

import { api } from "@/lib/api";
import type { QuizData, QuizAttemptResult } from "@/pages/assistant/types";
import { useAuthStore } from "@/store/useAuthStore";

export interface GenerateQuizRequest {
  dataset_ids: string[];
  topic?: string;
  question_count?: number;
  difficulty?: string;
  language?: string;
  model_id?: string;
}

export async function generateQuiz(data: GenerateQuizRequest): Promise<QuizData> {
  const { data: result } = await api.post<QuizData>("/api/v1/assistant/quiz/generate", data);
  return result;
}

export async function getQuiz(quizId: string): Promise<QuizData> {
  const { data } = await api.get<QuizData>(`/api/v1/assistant/quiz/${quizId}`);
  return data;
}

export async function submitQuiz(
  quizId: string,
  answers: Record<string, string>,
): Promise<QuizAttemptResult> {
  const { data } = await api.post<QuizAttemptResult>(
    `/api/v1/assistant/quiz/${quizId}/submit`,
    { answers },
  );
  return data;
}

/**
 * Anonymous quiz submit for the public /share/:shareCode page.
 * No auth required — rate-limited server-side by IP + ag_anon_id cookie.
 * Repeat submissions for the same (shareCode, anon, quizId) return the
 * cached first attempt with `cached: true` appended.
 */
export async function submitSharedQuiz(
  shareCode: string,
  quizId: string,
  answers: Record<string, string>,
): Promise<QuizAttemptResult & { cached?: boolean }> {
  const resp = await fetch(
    `/api/v1/assistant/shares/${encodeURIComponent(shareCode)}/quiz/${encodeURIComponent(quizId)}/submit`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include", // send ag_anon_id cookie
      body: JSON.stringify({ answers }),
    },
  );
  if (!resp.ok) {
    throw new Error(`Share quiz submit failed: ${resp.status}`);
  }
  return resp.json();
}

export async function listQuizzes(params?: {
  limit?: number;
  offset?: number;
}): Promise<{ quizzes: QuizData[]; total: number }> {
  const { data } = await api.get<{ quizzes: QuizData[]; total: number }>(
    "/api/v1/assistant/quiz/list",
    { params },
  );
  return data;
}

export async function generateQuizStream(
  data: GenerateQuizRequest,
  onStatus: (message: string) => void,
  onReady: (quiz: QuizData) => void,
  onError: (message: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const bearerToken = useAuthStore.getState().token || "";

  const resp = await fetch("/api/v1/assistant/quiz/generate/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(bearerToken ? { Authorization: `Bearer ${bearerToken}` } : {}),
    },
    body: JSON.stringify(data),
    signal,
  });

  if (!resp.ok || !resp.body) {
    onError(`Generation failed: ${resp.status}`);
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";

    for (const part of parts) {
      const dataLine = part.split("\n").find((l) => l.startsWith("data:"));
      if (!dataLine) continue;
      const jsonStr = dataLine.slice(5).trim();
      if (!jsonStr || jsonStr === "[DONE]") continue;

      try {
        const event = JSON.parse(jsonStr);
        switch (event.event_type) {
          case "quiz:status":
            onStatus(event.data?.message || "");
            break;
          case "quiz:ready":
            onReady(event.data);
            break;
          case "quiz:error":
            onError(event.data?.message || "Generation failed");
            break;
        }
      } catch {
        // Skip malformed events
      }
    }
  }
}

export async function deleteQuiz(quizId: string): Promise<void> {
  await api.delete(`/api/v1/assistant/quiz/${quizId}`);
}

// --- Share ---

export interface ShareQuizRequest {
  expires_hours?: number;
  max_attempts?: number;
  require_name?: boolean;
}

export interface ShareQuizResponse {
  share_id: string;
  share_code: string;
  quiz_id: string;
  quiz_title: string;
  expires_at: string | null;
  require_name: boolean;
  max_attempts: number | null;
}

export async function createQuizShare(
  quizId: string,
  data?: ShareQuizRequest,
): Promise<ShareQuizResponse> {
  const { data: result } = await api.post<ShareQuizResponse>(
    `/api/v1/assistant/quiz/${quizId}/share`,
    data ?? {},
  );
  return result;
}

export interface QuizAttemptSummary {
  attempt_id: string;
  user_id: string | null;
  display_name: string | null;
  total_score: number | null;
  correct_count: number | null;
  total_count: number | null;
  started_at: string | null;
  completed_at: string | null;
  status: string;
}

export async function listAttempts(
  quizId: string,
): Promise<{ attempts: QuizAttemptSummary[] }> {
  const { data } = await api.get<{ attempts: QuizAttemptSummary[] }>(
    `/api/v1/assistant/quiz/${quizId}/attempts`,
  );
  return data;
}

export async function revokeQuizShare(
  quizId: string,
  shareId: string,
): Promise<void> {
  await api.delete(`/api/v1/assistant/quiz/${quizId}/share/${shareId}`);
}
