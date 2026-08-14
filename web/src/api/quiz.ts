/**
 * Quiz API client — generate, fetch, submit, and list quizzes.
 */

import { api } from "@/lib/api";
import type { QuizData, QuizAttemptResult } from "@/pages/assistant/types";

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
