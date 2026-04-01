/**
 * Exam Management API client.
 */

import { api } from "@/lib/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Exam {
  exam_id: string;
  quiz_id: string;
  title: string;
  description?: string;
  status: "draft" | "published" | "closed" | "archived";
  published_by: string;
  question_count: number;
  attempt_count: number;
  avg_score: number | null;
  deadline: string | null;
  max_retakes: number;
  time_limit_minutes: number | null;
  passing_score: number | null;
  share_id: string | null;
  share_code?: string;
  assigned_groups?: string[];
  settings?: Record<string, unknown>;
  created_at: string;
  updated_at?: string;
}

export interface ExamAttempt {
  attempt_id: string;
  user_id: string | null;
  display_name: string | null;
  total_score: number | null;
  correct_count: number | null;
  total_count: number | null;
  started_at: string | null;
  completed_at: string | null;
  status: string;
  client_ip?: string | null;
}

export interface ExamStats {
  total_participants: number;
  avg_score: number | null;
  min_score: number | null;
  max_score: number | null;
  passed: number;
  pass_rate: number;
  passing_score: number;
  score_distribution: Record<string, number>;
  per_question: Array<{
    question_num: number;
    question_text: string;
    correct_answer: string;
    correct_rate: number;
    total_answered: number;
    most_common_wrong: string | null;
  }>;
}

export interface ExamReport {
  report_id: string;
  exam_id?: string;
  content: string;
  model_id: string;
  generated_at: string;
}

export interface CreateExamRequest {
  quiz_id: string;
  title: string;
  description?: string;
  assigned_groups?: string[];
  deadline?: string;
  max_retakes?: number;
  time_limit_minutes?: number;
  passing_score?: number;
}

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

export async function createExam(data: CreateExamRequest) {
  const { data: result } = await api.post<{ exam_id: string }>("/api/v1/exams", data);
  return result;
}

export async function listExams(params?: {
  status?: string;
  limit?: number;
  offset?: number;
}) {
  const { data } = await api.get<{ exams: Exam[]; total: number }>("/api/v1/exams", { params });
  return data;
}

export async function getExam(examId: string) {
  const { data } = await api.get<Exam>(`/api/v1/exams/${examId}`);
  return data;
}

export async function updateExam(examId: string, data: Partial<CreateExamRequest>) {
  return api.patch(`/api/v1/exams/${examId}`, data);
}

export async function publishExam(examId: string) {
  const { data } = await api.post<{ status: string; share_code: string }>(`/api/v1/exams/${examId}/publish`);
  return data;
}

export async function closeExam(examId: string) {
  return api.post(`/api/v1/exams/${examId}/close`);
}

export async function listExamAttempts(examId: string, params?: { limit?: number; offset?: number }) {
  const { data } = await api.get<{ attempts: ExamAttempt[]; total: number }>(
    `/api/v1/exams/${examId}/attempts`,
    { params },
  );
  return data;
}

export async function getExamStats(examId: string) {
  const { data } = await api.get<ExamStats>(`/api/v1/exams/${examId}/stats`);
  return data;
}

export async function analyzeExam(examId: string) {
  const { data } = await api.post<{ report_id: string; content: string }>(`/api/v1/exams/${examId}/analyze`);
  return data;
}

export async function listExamReports(examId: string) {
  const { data } = await api.get<{ reports: Array<{ report_id: string; model_id: string; generated_at: string }> }>(
    `/api/v1/exams/${examId}/reports`,
  );
  return data;
}

export async function getExamReport(examId: string, reportId: string) {
  const { data } = await api.get<ExamReport>(`/api/v1/exams/${examId}/reports/${reportId}`);
  return data;
}

export function getExamExportUrl(examId: string) {
  return `/api/v1/exams/${examId}/attempts/export`;
}
