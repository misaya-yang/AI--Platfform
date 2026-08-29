import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useQueryClient } from "@tanstack/react-query";

import {
  batchUploadDocuments,
  uploadDocument,
  updateDatasetConfig,
  type ProcessingMode,
} from "@/api/knowledge";
import { toast } from "@/hooks/use-toast";
import { DEFAULT_CHUNKING_CONFIG } from "@/types/knowledge";

export const DATASET_EMBEDDING_MODELS = [
  {
    provider: "gemini",
    model: "gemini-embedding-2-preview",
    label: "Gemini Embedding 2 Preview",
    dimension: 1024,
  },
  {
    provider: "gemini",
    model: "gemini-embedding-001",
    label: "Gemini Embedding 001",
    dimension: 1024,
  },
  {
    provider: "dashscope",
    model: "text-embedding-v4",
    label: "DashScope text-embedding-v4",
    dimension: 1024,
    badge: "knowledge.upload.recommended",
  },
  {
    provider: "dashscope",
    model: "text-embedding-v3",
    label: "DashScope text-embedding-v3",
    dimension: 1024,
  },
  {
    provider: "siliconflow",
    model: "BAAI/bge-m3",
    label: "SiliconFlow BGE-M3",
    dimension: 1024,
  },
  {
    provider: "siliconflow",
    model: "BAAI/bge-large-zh-v1.5",
    label: "SiliconFlow BGE-Large-ZH",
    dimension: 1024,
  },
] as const;

interface DatasetUploadControllerOptions {
  datasetId?: string;
  pendingFiles: File[];
  onOpenChange: (open: boolean) => void;
  onPendingFilesChange: (files: File[]) => void;
  onUploadingChange: (uploading: boolean) => void;
}

export function useDatasetUploadController({
  datasetId,
  pendingFiles,
  onOpenChange,
  onPendingFilesChange,
  onUploadingChange,
}: DatasetUploadControllerOptions) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [uploadChunkMode, setUploadChunkMode] = useState(DEFAULT_CHUNKING_CONFIG.mode);
  const [uploadChunkSize, setUploadChunkSize] = useState(DEFAULT_CHUNKING_CONFIG.chunk_size);
  const [uploadChunkOverlap, setUploadChunkOverlap] = useState(DEFAULT_CHUNKING_CONFIG.chunk_overlap);
  const [uploadMinParagraphLength, setUploadMinParagraphLength] = useState(50);
  const [uploadMergeShortParagraphs, setUploadMergeShortParagraphs] = useState(true);
  const [uploadHeadingLevel, setUploadHeadingLevel] = useState<"h1" | "h2" | "h3">("h2");
  const [uploadParentChunkSize, setUploadParentChunkSize] = useState(1500);
  const [uploadChildChunkSize, setUploadChildChunkSize] = useState(300);
  const [uploadChildOverlap, setUploadChildOverlap] = useState(50);
  const [uploadSeparator, setUploadSeparator] = useState("\\n\\n");
  const [uploadKeepSeparator, setUploadKeepSeparator] = useState(false);
  const [uploadRegexPattern, setUploadRegexPattern] = useState("");
  const [uploadQuestionPrefix, setUploadQuestionPrefix] = useState("Q:");
  const [uploadAnswerPrefix, setUploadAnswerPrefix] = useState("A:");
  const [uploadMetadataEnabled, setUploadMetadataEnabled] = useState(false);
  const [uploadExtractTitle, setUploadExtractTitle] = useState(true);
  const [uploadExtractSummary, setUploadExtractSummary] = useState(false);
  const [uploadExtractKeywords, setUploadExtractKeywords] = useState(true);
  const [uploadExtractEntities, setUploadExtractEntities] = useState(false);
  const [uploadDetectLanguage, setUploadDetectLanguage] = useState(true);
  const [uploadTableEnabled, setUploadTableEnabled] = useState(false);
  const [uploadTableMode, setUploadTableMode] = useState<
    "markdown" | "row_based" | "structured"
  >("markdown");
  const [uploadTableIncludeHeaders, setUploadTableIncludeHeaders] = useState(true);
  const [uploadTableGenerateSummary, setUploadTableGenerateSummary] = useState(false);
  const [uploadEmbeddingModel, setUploadEmbeddingModel] = useState(
    "dashscope:text-embedding-v4"
  );
  // NOTE: rerank defaults ON here but OFF in the platform defaults
  // (DEFAULT_RETRIEVAL_CONFIG.rerank.enabled). The upload dialog has always
  // shipped this way; changing it is a product decision, tracked for C3.
  const [rerankEnabled, setRerankEnabled] = useState(true);
  const [rerankModel, setRerankModel] = useState(DEFAULT_RETRIEVAL_CONFIG.rerank.model);
  const uploadProcessingMode: ProcessingMode = "text_only";

  function buildChunkingConfig() {
    const baseConfig: Record<string, unknown> = { mode: uploadChunkMode };

    switch (uploadChunkMode) {
      case "fixed_size":
        baseConfig.chunk_size = uploadChunkSize;
        baseConfig.chunk_overlap = uploadChunkOverlap;
        break;
      case "paragraph":
        baseConfig.chunk_size = uploadChunkSize;
        baseConfig.min_paragraph_length = uploadMinParagraphLength;
        baseConfig.merge_short_paragraphs = uploadMergeShortParagraphs;
        break;
      case "heading":
        baseConfig.chunk_size = uploadChunkSize;
        baseConfig.heading_level = uploadHeadingLevel;
        break;
      case "hierarchical":
        baseConfig.parent_chunk_size = uploadParentChunkSize;
        baseConfig.child_chunk_size = uploadChildChunkSize;
        baseConfig.child_overlap = uploadChildOverlap;
        break;
      case "separator":
        baseConfig.chunk_size = uploadChunkSize;
        baseConfig.primary_separator = uploadSeparator;
        baseConfig.keep_separator = uploadKeepSeparator;
        break;
      case "regex":
        baseConfig.chunk_size = uploadChunkSize;
        baseConfig.regex_pattern = uploadRegexPattern;
        break;
      case "qa":
        baseConfig.question_prefix = uploadQuestionPrefix;
        baseConfig.answer_prefix = uploadAnswerPrefix;
        break;
      case "recursive":
        baseConfig.chunk_size = uploadChunkSize;
        baseConfig.chunk_overlap = uploadChunkOverlap;
        break;
      default:
        break;
    }

    if (uploadMetadataEnabled) {
      baseConfig.extract_metadata = true;
      const fields: string[] = [];
      if (uploadExtractTitle) fields.push("title");
      if (uploadExtractKeywords) fields.push("keywords");
      if (uploadDetectLanguage) fields.push("language");
      if (uploadExtractSummary) fields.push("summary");
      if (uploadExtractEntities) fields.push("entities");
      fields.push("date", "word_count", "char_count");
      baseConfig.metadata_fields = fields;
    }

    return baseConfig;
  }

  async function handleConfirmUpload() {
    if (!datasetId || pendingFiles.length === 0) return;

    const filesToUpload = [...pendingFiles];
    onUploadingChange(true);

    try {
      const chunkingConfig = buildChunkingConfig();
      const [embeddingProvider, embeddingModel] = uploadEmbeddingModel.split(":");
      const selectedModel = DATASET_EMBEDDING_MODELS.find(
        (model) => model.provider === embeddingProvider && model.model === embeddingModel
      );

      try {
        await updateDatasetConfig(datasetId, {
          chunking_config: chunkingConfig as typeof chunkingConfig & { mode: "automatic" },
          retrieval_config: { rerank: { enabled: rerankEnabled, model: rerankModel } },
          embedding_provider: embeddingProvider,
          embedding_model: embeddingModel,
          embedding_dimension: selectedModel?.dimension || 1024,
        });
        await new Promise((resolve) => setTimeout(resolve, 200));
      } catch (configError) {
        console.warn("Config update failed (non-blocking):", configError);
      }

      onOpenChange(false);
      onPendingFilesChange([]);
      onUploadingChange(false);

      if (filesToUpload.length >= 3) {
        try {
          const result = await batchUploadDocuments(datasetId, filesToUpload);
          await queryClient.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
          if (result.rejected > 0) {
            toast.warning(
              t("knowledge.detail.batchUploadDone", {
                success: result.accepted,
                failed: result.rejected,
              }),
              result.errors.map((error) => `${error.filename}: ${error.error}`).join("; ")
            );
          } else {
            toast.success(
              t("knowledge.detail.batchUploadSuccess", { count: result.accepted }),
              t("knowledge.detail.batchProcessing")
            );
          }
        } catch (error) {
          console.error("Batch upload failed:", error);
          toast.error(
            t("knowledge.detail.uploadFailed"),
            error instanceof Error ? error.message : String(error)
          );
        }
      } else {
        let successCount = 0;
        let failCount = 0;
        for (const file of filesToUpload) {
          try {
            await uploadDocument(datasetId, file, uploadProcessingMode);
            successCount += 1;
          } catch (error) {
            failCount += 1;
            console.error(`Upload failed for ${file.name}:`, error);
          }
          await queryClient.invalidateQueries({ queryKey: ["kb-documents", datasetId] });
        }

        if (failCount > 0) {
          toast.warning(
            t("knowledge.detail.uploadDone", { success: successCount, failed: failCount })
          );
        } else if (successCount > 0) {
          toast.success(
            t("knowledge.detail.filesUploaded", { count: successCount }),
            t("knowledge.detail.docProcessing")
          );
        }
      }
    } catch (error) {
      console.error("Upload failed:", error);
      onOpenChange(false);
      onPendingFilesChange([]);
      onUploadingChange(false);
      toast.error(
        t("knowledge.detail.uploadFailed"),
        error instanceof Error ? error.message : String(error)
      );
    }
  }

  return {
    uploadChunkMode,
    setUploadChunkMode,
    uploadChunkSize,
    setUploadChunkSize,
    uploadChunkOverlap,
    setUploadChunkOverlap,
    uploadMinParagraphLength,
    setUploadMinParagraphLength,
    uploadMergeShortParagraphs,
    setUploadMergeShortParagraphs,
    uploadHeadingLevel,
    setUploadHeadingLevel,
    uploadParentChunkSize,
    setUploadParentChunkSize,
    uploadChildChunkSize,
    setUploadChildChunkSize,
    uploadChildOverlap,
    setUploadChildOverlap,
    uploadSeparator,
    setUploadSeparator,
    uploadKeepSeparator,
    setUploadKeepSeparator,
    uploadRegexPattern,
    setUploadRegexPattern,
    uploadQuestionPrefix,
    setUploadQuestionPrefix,
    uploadAnswerPrefix,
    setUploadAnswerPrefix,
    uploadMetadataEnabled,
    setUploadMetadataEnabled,
    uploadExtractTitle,
    setUploadExtractTitle,
    uploadExtractSummary,
    setUploadExtractSummary,
    uploadExtractKeywords,
    setUploadExtractKeywords,
    uploadExtractEntities,
    setUploadExtractEntities,
    uploadDetectLanguage,
    setUploadDetectLanguage,
    uploadTableEnabled,
    setUploadTableEnabled,
    uploadTableMode,
    setUploadTableMode,
    uploadTableIncludeHeaders,
    setUploadTableIncludeHeaders,
    uploadTableGenerateSummary,
    setUploadTableGenerateSummary,
    uploadEmbeddingModel,
    setUploadEmbeddingModel,
    rerankEnabled,
    setRerankEnabled,
    rerankModel,
    setRerankModel,
    handleConfirmUpload,
  };
}
