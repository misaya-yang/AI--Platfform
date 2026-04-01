/**
 * Quiz API client — generate, fetch, submit, and list quizzes.
 */

import api from "@/lib/api";
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

export async function revokeQuizShare(
  quizId: string,
  shareId: string,
): Promise<void> {
  await api.delete(`/api/v1/assistant/quiz/${quizId}/share/${shareId}`);
}
