export interface PendingFile {
  id: string;
  file: File;
  name: string;
  size: number;
  status: "pending" | "uploading" | "done" | "error";
  error?: string;
}

export interface PendingUrl {
  id: string;
  url: string;
  title: string;
  status: "pending" | "uploading" | "done" | "error";
  error?: string;
}

export const EMBEDDING_MODELS = [
  {
    provider: "gemini",
    model: "gemini-embedding-001",
    nameKey: "knowledge.create.embeddingGemini001",
    dimension: 1024,
  },
  {
    provider: "dashscope",
    model: "text-embedding-v4",
    nameKey: "knowledge.create.embeddingDashscopeV4",
    dimension: 1024,
  },
  {
    provider: "dashscope",
    model: "text-embedding-v3",
    nameKey: "knowledge.create.embeddingDashscopeV3",
    dimension: 1024,
  },
  {
    provider: "dashscope",
    model: "text-embedding-v2",
    nameKey: "knowledge.create.embeddingDashscopeV2",
    dimension: 1536,
  },
  {
    provider: "siliconflow",
    model: "BAAI/bge-m3",
    nameKey: "knowledge.create.embeddingBgeM3",
    dimension: 1024,
  },
  {
    provider: "siliconflow",
    model: "Pro/BAAI/bge-m3",
    nameKey: "knowledge.create.embeddingBgeM3Pro",
    dimension: 1024,
  },
  {
    provider: "siliconflow",
    model: "BAAI/bge-large-zh-v1.5",
    nameKey: "knowledge.create.embeddingBgeLargeZh15",
    dimension: 1024,
  },
  {
    provider: "siliconflow",
    model: "BAAI/bge-large-en-v1.5",
    nameKey: "knowledge.create.embeddingBgeLargeEn15",
    dimension: 1024,
  },
  {
    provider: "siliconflow",
    model: "netease-youdao/bce-embedding-base_v1",
    nameKey: "knowledge.create.embeddingBceBase",
    dimension: 512,
  },
] as const;

export type VisibilityType = "private" | "tenant" | "public";
export type KBType = "document" | "data";
export type UseCase = "basic_qa" | "rich_text_response";

export const MAX_NAME_LENGTH = 100;
export const MAX_FILE_SIZE = 100 * 1024 * 1024;
export const SUPPORTED_FILE_EXTENSIONS = /\.(pdf|docx|txt|md|html)$/i;
export const URL_PATTERN = /^https?:\/\/([\w-]+\.)+[\w-]+(\/[\w\-./?%&=#]*)?$/i;
