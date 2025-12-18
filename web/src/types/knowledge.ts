export type DatasetVisibility = "private" | "tenant" | "public";
export type DatasetPermission = "owner" | "editor" | "viewer";

export interface Dataset {
  dataset_id: string;
  name: string;
  description?: string;
  tenant_id?: string;
  visibility: DatasetVisibility;
  embedding_provider: string;
  embedding_model: string;
  embedding_dimension: number;
  collection_name?: string | null;
  my_permission?: DatasetPermission;
  created_at?: string;
  updated_at?: string;
}

export type DocumentStatus =
  | "uploaded"
  | "parsing"
  | "segmenting"
  | "embedding"
  | "completed"
  | "failed";

export interface Document {
  document_id: string;
  dataset_id: string;
  title: string;
  source_type: string;
  source_uri?: string | null;
  mime_type?: string | null;
  size_bytes?: number | null;
  status: DocumentStatus;
  progress: number;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface Segment {
  segment_id: string;
  dataset_id: string;
  document_id: string;
  position: number;
  text: string;
  token_count: number;
  vector_id?: string | null;
  created_at?: string;
  updated_at?: string;
}

export type RetrieveMode = "keyword" | "hybrid" | "vector";

export interface RetrieveRequest {
  query: string;
  top_k?: number;
  mode?: RetrieveMode;
  document_id?: string | null;
  alpha?: number | null;

  vector_top_k?: number;
  keyword_top_k?: number;
  candidate_top_k?: number;
  keyword_candidate_k?: number;

  fusion?: "rrf" | "alpha";
  rrf_k?: number;
  rrf_weights?: Record<string, number>;

  rerank?: boolean;
  rerank_model?: string;
  rerank_top_n?: number;

  mmr?: boolean;
  mmr_lambda?: number;
  mmr_threshold?: number | null;
}

export interface RetrieveHit {
  segment_id: string;
  document_id: string;
  score: number;
  text: string;
  metadata: Record<string, unknown>;
}

export interface RetrieveResponse {
  results: RetrieveHit[];
  metadata: Record<string, unknown>;
}
