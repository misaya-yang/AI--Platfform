import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQueryClient } from "@tanstack/react-query";

import {
  batchUploadDocuments,
  uploadDocument,
  updateDatasetConfig,
  type ProcessingMode,
} from "@/api/knowledge";
import { toast } from "@/hooks/use-toast";
import { getSourceUploadError } from "@/pages/knowledge/create/datasetCreateModel";
import { uploadDatasetFiles } from "@/pages/knowledge/detail/datasetUploadModel";
import { DEFAULT_CHUNKING_CONFIG, DEFAULT_RETRIEVAL_CONFIG, type Dataset } from "@/types/knowledge";

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
  dataset?: Dataset;
  pendingFiles: File[];
  onOpenChange: (open: boolean) => void;
  onPendingFilesChange: (files: File[]) => void;
  onUploadingChange: (uploading: boolean) => void;
}

export function useDatasetUploadController({
  datasetId,
  dataset,
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

  // Adopt the dataset's current embedding as the dialog selection once it is
  // known — confirming an upload used to silently swap the dataset's model
  // for the hard-coded default below.
  const datasetEmbedding =
    dataset?.embedding_provider && dataset?.embedding_model
      ? `${dataset.embedding_provider}:${dataset.embedding_model}`
      : null;
  useEffect(() => {
    if (datasetEmbedding) setUploadEmbeddingModel(datasetEmbedding);
  }, [datasetEmbedding]);

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
      const metadataFields = [
        uploadExtractTitle && "title",
        uploadExtractSummary && "summary",
        uploadExtractKeywords && "keywords",
        uploadExtractEntities && "entities",
        uploadDetectLanguage && "language",
      ].filter((field): field is string => Boolean(field));
      baseConfig.extract_metadata = metadataFields.length > 0;
      baseConfig.metadata_fields = metadataFields;
    } else {
      baseConfig.extract_metadata = false;
      baseConfig.metadata_fields = [];
    }

    return baseConfig;
  }

  /**
   * Mirror the ChunkingConfigSchema validators so a doomed config update is
   * blocked before the upload starts. Modes that omit chunk_size/
   * chunk_overlap fall back to the schema defaults (2000 / 300) for the
   * check.
   */
  function chunkingValidationError(): string | null {
    if (uploadChunkMode === "regex") {
      return t("knowledge.detail.chunkRegexDisabled");
    }
    const sendsSize =
      uploadChunkMode === "fixed_size" ||
      uploadChunkMode === "paragraph" ||
      uploadChunkMode === "heading" ||
      uploadChunkMode === "separator" ||
      uploadChunkMode === "recursive";
    const sendsOverlap =
      uploadChunkMode === "fixed_size" || uploadChunkMode === "recursive";
    const effectiveSize = sendsSize ? uploadChunkSize : 2000;
    const effectiveOverlap = sendsOverlap ? uploadChunkOverlap : 300;
    if (effectiveOverlap >= effectiveSize) {
      return t("knowledge.detail.chunkOverlapTooLarge");
    }
    if (uploadChunkMode === "hierarchical" && uploadChildOverlap >= uploadChildChunkSize) {
      return t("knowledge.detail.childOverlapTooLarge");
    }
    return null;
  }

  async function handleConfirmUpload() {
    if (!datasetId || pendingFiles.length === 0) return;

    const validationError = chunkingValidationError();
    if (validationError) {
      toast.error(t("knowledge.detail.uploadConfigInvalid"), validationError);
      return;
    }

    const filesToUpload = [...pendingFiles];
    onUploadingChange(true);

    try {
      const chunkingConfig = buildChunkingConfig();
      const [embeddingProvider, embeddingModel] = uploadEmbeddingModel.split(":");
      const selectedModel = DATASET_EMBEDDING_MODELS.find(
        (model) => model.provider === embeddingProvider && model.model === embeddingModel
      );
      // Only touch the dataset's embedding when the selection actually
      // differs — the PUT would otherwise re-set (or swap) it on every
      // upload.
      const embeddingChanged =
        !datasetEmbedding || datasetEmbedding !== uploadEmbeddingModel;

      const configPatch: Parameters<typeof updateDatasetConfig>[1] = {
        chunking_config: chunkingConfig,
        retrieval_config: { rerank: { enabled: rerankEnabled, model: rerankModel } },
      };
      if (embeddingChanged) {
        configPatch.embedding_provider = embeddingProvider;
        configPatch.embedding_model = embeddingModel;
        configPatch.embedding_dimension = selectedModel?.dimension || 1024;
      }
      await updateDatasetConfig(datasetId, configPatch);

      const describeError = (error: unknown) =>
        getSourceUploadError(error, {
          fallback: t("knowledge.detail.uploadFailed"),
          requestTooLarge: t("knowledge.create.uploadTooLarge"),
        });
      const outcome = await uploadDatasetFiles(filesToUpload, {
        uploadBatch: (files) => batchUploadDocuments(datasetId, files),
        uploadOne: (file) => uploadDocument(datasetId, file, uploadProcessingMode),
        describeError,
      });

      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["kb-documents", datasetId] }),
        queryClient.invalidateQueries({ queryKey: ["kb-dataset", datasetId] }),
        queryClient.invalidateQueries({ queryKey: ["kb-dataset-sources", datasetId] }),
      ]);

      if (outcome.failures.length > 0) {
        onPendingFilesChange(outcome.failures.map(({ file }) => file));
        toast.warning(
          t("knowledge.detail.uploadDone", {
            success: outcome.accepted,
            failed: outcome.failures.length,
          }),
          outcome.failures.map(({ file, error }) => `${file.name}: ${error}`).join("; ")
        );
        return;
      }

      onPendingFilesChange([]);
      onOpenChange(false);
      toast.success(
        t("knowledge.detail.filesUploaded", { count: outcome.accepted }),
        t("knowledge.detail.docProcessing")
      );
    } catch (error) {
      console.error("Upload failed:", error);
      toast.error(
        t("knowledge.detail.uploadFailed"),
        getSourceUploadError(error, {
          fallback: t("knowledge.detail.uploadFailed"),
          requestTooLarge: t("knowledge.create.uploadTooLarge"),
        })
      );
    } finally {
      onUploadingChange(false);
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
