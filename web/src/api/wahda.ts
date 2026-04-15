/**
 * Wahda API client — Islamic recommendation features.
 *
 * All routes proxy through the gateway: /api/v1/islamic/wahda/*
 */
import { api } from "@/lib/api";

export interface RecommendationResponse {
  type: string | null;
  event_name: string | null;
  questions: string[];
  date?: string;
}

export interface TypeaheadResponse {
  suggestions: string[];
}

export interface TrendingResponse {
  questions: string[];
  period: string;
  source: string;
}

/** Seasonal / daily recommended questions */
export async function getRecommendations(): Promise<RecommendationResponse> {
  const { data } = await api.get<RecommendationResponse>(
    "/api/v1/islamic/wahda/recommendations"
  );
  return data;
}

/** Type-ahead suggestions for a query prefix */
export async function getTypeahead(prefix: string): Promise<string[]> {
  const { data } = await api.get<TypeaheadResponse>(
    `/api/v1/islamic/wahda/typeahead`,
    { params: { q: prefix } }
  );
  return data.suggestions || [];
}

/** Get full question pool for client-side typeahead filtering */
export async function getTypeaheadPool(): Promise<string[]> {
  const { data } = await api.get<{ questions: string[] }>(
    "/api/v1/islamic/wahda/typeahead/pool"
  );
  return data.questions || [];
}

/** Trending / frequently asked questions (past 7 days) */
export async function getTrending(): Promise<TrendingResponse> {
  const { data } = await api.get<TrendingResponse>(
    "/api/v1/islamic/wahda/trending"
  );
  return data;
}
