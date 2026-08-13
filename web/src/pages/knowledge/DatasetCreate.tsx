/**
 * Knowledge Base Creation Wizard
 *
 * 3-Step wizard following Alibaba Cloud design:
 * 1. Basic Info - Name, description, embedding model
 * 2. Select Data - Upload files / URL
 * 3. Index Settings - Chunking, retrieval config
 */

import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { message } from "antd";
import { AlertCircle, ArrowLeft, ArrowRight, Check, Loader2 } from "lucide-react";

import { createDataset, createDocumentFromUrl, uploadDocument } from "@/api/knowledge";
import { Button } from "@/components/ui/button";
import { DatasetCreateBasicStep } from "@/pages/knowledge/create/DatasetCreateBasicStep";
import { DatasetCreateIndexStep } from "@/pages/knowledge/create/DatasetCreateIndexStep";
import { DatasetCreateSourcesStep } from "@/pages/knowledge/create/DatasetCreateSourcesStep";
import {
  EMBEDDING_MODELS,
  MAX_FILE_SIZE,
  MAX_NAME_LENGTH,
  SUPPORTED_FILE_EXTENSIONS,
  URL_PATTERN,
  type KBType,
  type PendingFile,
  type PendingUrl,
  type UseCase,
  type VisibilityType,
} from "@/pages/knowledge/create/datasetCreateModel";
import type { ChunkingMode } from "@/types/knowledge";

export default function DatasetCreatePage() {
  const navigate = useNavigate();
  const { t } = useTranslation();

  const [step, setStep] = useState(1);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createdDatasetId, setCreatedDatasetId] = useState<string | null>(null);
  const [nameError, setNameError] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState<VisibilityType>("private");
  const [kbType, setKbType] = useState<KBType>("document");
  const [useCase, setUseCase] = useState<UseCase>("basic_qa");
  const [embeddingModel, setEmbeddingModel] = useState("dashscope:text-embedding-v4");

  const [pendingFiles, setPendingFiles] = useState<PendingFile[]>([]);
  const [pendingUrls, setPendingUrls] = useState<PendingUrl[]>([]);
  const [urlInput, setUrlInput] = useState("");
  const [urlTitle, setUrlTitle] = useState("");

  const [chunkingMode, setChunkingMode] = useState<ChunkingMode>("automatic");
  const [maxChunkSize, setMaxChunkSize] = useState(600);
  const [metadataExtract, setMetadataExtract] = useState(false);
  const [excelHeaderConcat, setExcelHeaderConcat] = useState(false);
  const [multiTurnRewrite, setMultiTurnRewrite] = useState(true);
  const [rerankModel, setRerankModel] = useState("default");
  const [scoreThreshold, setScoreThreshold] = useState(0.2);
  const [maxRecall, setMaxRecall] = useState(5);

  const handleChunkingModeSelect = useCallback((mode: ChunkingMode) => {
    setChunkingMode(mode);
  }, []);

  const handleFilesSelect = useCallback(
    (files: FileList | null) => {
      if (!files) return;
      const newFiles: PendingFile[] = [];
      const errors: string[] = [];

      Array.from(files).forEach((file) => {
        if (!SUPPORTED_FILE_EXTENSIONS.test(file.name)) {
          errors.push(t("knowledge.create.validation.unsupportedFile", { name: file.name }));
          return;
        }
        const maxSize = MAX_FILE_SIZE;
        const maxSizeLabel = "100MB";

        if (file.size > maxSize) {
          errors.push(
            t("knowledge.create.validation.fileTooLarge", {
              name: file.name,
              limit: maxSizeLabel,
            })
          );
          return;
        }

        if (pendingFiles.some((pendingFile) => pendingFile.name === file.name && pendingFile.size === file.size)) {
          errors.push(t("knowledge.create.validation.duplicateFile", { name: file.name }));
          return;
        }

        newFiles.push({
          id: `file_${Date.now()}_${Math.random().toString(36).slice(2)}`,
          file,
          name: file.name,
          size: file.size,
          status: "pending",
        });
      });

      if (errors.length > 0) {
        errors.forEach((validationError) => message.warning(validationError));
      }

      if (newFiles.length > 0) {
        setPendingFiles((previous) => [...previous, ...newFiles]);
      }
    },
    [pendingFiles, t]
  );

  const handleRemoveFile = useCallback((id: string) => {
    setPendingFiles((previous) => previous.filter((file) => file.id !== id));
  }, []);

  const handleAddUrl = useCallback(() => {
    const trimmedUrl = urlInput.trim();
    if (!trimmedUrl) return;

    if (!URL_PATTERN.test(trimmedUrl)) {
      message.error(t("knowledge.create.validation.invalidUrl"));
      return;
    }

    if (pendingUrls.some((pendingUrl) => pendingUrl.url === trimmedUrl)) {
      message.warning(t("knowledge.create.validation.duplicateUrl"));
      return;
    }

    const newUrl: PendingUrl = {
      id: `url_${Date.now()}_${Math.random().toString(36).slice(2)}`,
      url: trimmedUrl,
      title: urlTitle.trim() || trimmedUrl,
      status: "pending",
    };
    setPendingUrls((previous) => [...previous, newUrl]);
    setUrlInput("");
    setUrlTitle("");
  }, [urlInput, urlTitle, pendingUrls, t]);

  const handleRemoveUrl = useCallback((id: string) => {
    setPendingUrls((previous) => previous.filter((url) => url.id !== id));
  }, []);

  const handleSubmit = async () => {
    setIsSubmitting(true);
    setError(null);

    try {
      const [provider, model] = embeddingModel.split(":");
      const selectedEmbeddingModel = EMBEDDING_MODELS.find(
        (candidate) => candidate.provider === provider && candidate.model === model
      );
      const rerankProvider = rerankModel.startsWith("bge-") ? "bge" : "dashscope";

      let datasetId = createdDatasetId;
      if (!datasetId) {
        const dataset = await createDataset({
          name: name.trim(),
          description: description.trim(),
          visibility,
          kb_type: kbType,
          use_case: useCase,
          embedding_provider: provider,
          embedding_model: model,
          embedding_dimension: selectedEmbeddingModel?.dimension || 1024,
          index_config: {
            chunking: {
              mode: chunkingMode,
              chunk_size: maxChunkSize,
              chunk_overlap: Math.min(50, Math.floor(maxChunkSize * 0.1)),
              extract_metadata: metadataExtract,
              remove_extra_spaces: true,
            },
            retrieval: {
              mode: "hybrid",
              top_k: maxRecall,
              score_threshold: scoreThreshold,
              rerank: {
                enabled: rerankModel !== "default",
                provider: rerankProvider,
                model: rerankModel === "default" ? "gte-rerank" : rerankModel,
              },
            },
          },
        });
        datasetId = dataset.dataset_id;
        setCreatedDatasetId(datasetId);
      }

      let failedUploads = 0;
      for (const pendingFile of pendingFiles.filter((file) => file.status !== "done")) {
        setPendingFiles((previous) =>
          previous.map((file) =>
            file.id === pendingFile.id ? { ...file, status: "uploading" } : file
          )
        );
        try {
          await uploadDocument(datasetId, pendingFile.file);
          setPendingFiles((previous) =>
            previous.map((file) =>
              file.id === pendingFile.id ? { ...file, status: "done" } : file
            )
          );
        } catch (uploadError) {
          failedUploads += 1;
          setPendingFiles((previous) =>
            previous.map((file) =>
              file.id === pendingFile.id
                ? {
                    ...file,
                    status: "error",
                    error:
                      uploadError instanceof Error
                        ? uploadError.message
                        : t("knowledge.create.uploadFailed"),
                  }
                : file
            )
          );
        }
      }

      for (const pendingUrl of pendingUrls.filter((url) => url.status !== "done")) {
        setPendingUrls((previous) =>
          previous.map((url) =>
            url.id === pendingUrl.id ? { ...url, status: "uploading" } : url
          )
        );
        try {
          await createDocumentFromUrl(datasetId, {
            url: pendingUrl.url,
            title: pendingUrl.title,
          });
          setPendingUrls((previous) =>
            previous.map((url) =>
              url.id === pendingUrl.id ? { ...url, status: "done" } : url
            )
          );
        } catch (fetchError) {
          failedUploads += 1;
          setPendingUrls((previous) =>
            previous.map((url) =>
              url.id === pendingUrl.id
                ? {
                    ...url,
                    status: "error",
                    error:
                      fetchError instanceof Error
                        ? fetchError.message
                        : t("knowledge.create.fetchFailed"),
                  }
                : url
            )
          );
        }
      }

      if (failedUploads > 0) {
        setError(
          t(
            "knowledge.create.partialUploadFailed",
            "{{count}} source(s) could not be added. The knowledge base was created; retry to upload only the failed sources.",
            { count: failedUploads }
          )
        );
        return;
      }

      navigate(`/knowledge/${datasetId}`);
    } catch (submitError) {
      console.error("Failed to create dataset:", submitError);
      setError(
        submitError instanceof Error ? submitError.message : t("knowledge.create.createError")
      );
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleNextStep = () => {
    if (step === 1) {
      const trimmedName = name.trim();
      if (!trimmedName) {
        setNameError(t("knowledge.create.nameRequired"));
        message.error(t("knowledge.create.nameRequired"));
        return;
      }
      if (trimmedName.length > MAX_NAME_LENGTH) {
        const validationError = t("knowledge.create.nameTooLong", {
          max: MAX_NAME_LENGTH,
        });
        setNameError(validationError);
        message.error(validationError);
        return;
      }
      setNameError(null);
      setStep(2);
    } else if (step === 2) {
      setStep(3);
    }
  };

  const wizardSteps = [
    { num: 1, label: t("knowledge.create.step1") },
    { num: 2, label: t("knowledge.create.step2") },
    { num: 3, label: t("knowledge.create.step3") },
  ];

  return (
    <div className="min-h-full bg-background">
      <div className="bg-card border-b px-4 py-4 sm:px-6">
        <div className="max-w-4xl mx-auto flex items-center gap-4">
          <button
            type="button"
            onClick={() => navigate("/knowledge")}
            aria-label={t("common.back", "Back")}
            className="text-muted-foreground hover:text-foreground/80 transition"
          >
            <ArrowLeft className="h-5 w-5" />
          </button>
          <div className="text-muted-foreground/70">/</div>
          <h1 className="text-lg font-semibold text-foreground">
            {t("knowledge.create.title")}
          </h1>
        </div>
      </div>

      <div className="bg-card border-b">
        <div className="max-w-4xl mx-auto px-4 py-4 sm:px-6 sm:py-6">
          <div className="flex items-center justify-between gap-3 sm:hidden" aria-live="polite">
            <span className="text-xs font-medium uppercase tracking-[0.12em] text-muted-foreground">
              {t("knowledge.create.stepProgress", "Step {{current}} of {{total}}", {
                current: step,
                total: wizardSteps.length,
              })}
            </span>
            <span className="text-sm font-semibold text-foreground">
              {wizardSteps[step - 1]?.label}
            </span>
          </div>
          <div className="hidden items-center justify-center gap-4 sm:flex">
            {wizardSteps.map((wizardStep, index) => (
              <div key={wizardStep.num} className="flex items-center">
                <div className="flex items-center gap-2">
                  <div
                    className={`w-7 h-7 rounded-full flex items-center justify-center text-sm font-medium transition-all ${
                      step > wizardStep.num
                        ? "bg-primary text-white"
                        : step === wizardStep.num
                          ? "bg-primary text-white ring-4 ring-primary/10"
                          : "bg-border text-muted-foreground"
                    }`}
                  >
                    {step > wizardStep.num ? (
                      <Check className="h-4 w-4" />
                    ) : (
                      wizardStep.num
                    )}
                  </div>
                  <span
                    className={`text-sm font-medium ${
                      step >= wizardStep.num ? "text-foreground" : "text-muted-foreground/70"
                    }`}
                  >
                    {wizardStep.label}
                  </span>
                </div>
                {index < 2 && (
                  <div
                    className={`w-24 h-0.5 mx-4 transition-all ${
                      step > wizardStep.num ? "bg-primary" : "bg-border"
                    }`}
                  />
                )}
              </div>
            ))}
          </div>
        </div>
      </div>

      <div className="max-w-4xl mx-auto px-4 py-6 sm:px-6 sm:py-8">
        {error && (
          <div className="mb-6 p-4 bg-red-500/10 dark:bg-red-500/15 border border-red-500/20 rounded-lg flex items-start gap-3">
            <AlertCircle className="h-5 w-5 text-red-500 shrink-0 mt-0.5" />
            <div>
              <p className="text-sm font-medium text-red-800 dark:text-red-300">
                {t("knowledge.create.createFailed")}
              </p>
              <p className="text-sm text-red-600 dark:text-red-400 mt-1">{error}</p>
            </div>
          </div>
        )}

        {step === 1 && (
          <DatasetCreateBasicStep
            name={name}
            nameError={nameError}
            description={description}
            visibility={visibility}
            kbType={kbType}
            useCase={useCase}
            embeddingModel={embeddingModel}
            onNameChange={(value) => {
              setName(value);
              if (nameError) setNameError(null);
            }}
            onDescriptionChange={setDescription}
            onVisibilityChange={setVisibility}
            onKbTypeChange={setKbType}
            onUseCaseChange={setUseCase}
            onEmbeddingModelChange={setEmbeddingModel}
          />
        )}

        {step === 2 && (
          <DatasetCreateSourcesStep
            pendingFiles={pendingFiles}
            pendingUrls={pendingUrls}
            urlInput={urlInput}
            urlTitle={urlTitle}
            onFilesSelect={handleFilesSelect}
            onRemoveFile={handleRemoveFile}
            onUrlInputChange={setUrlInput}
            onUrlTitleChange={setUrlTitle}
            onAddUrl={handleAddUrl}
            onRemoveUrl={handleRemoveUrl}
          />
        )}

        {step === 3 && (
          <DatasetCreateIndexStep
            chunkingMode={chunkingMode}
            maxChunkSize={maxChunkSize}
            metadataExtract={metadataExtract}
            excelHeaderConcat={excelHeaderConcat}
            multiTurnRewrite={multiTurnRewrite}
            rerankModel={rerankModel}
            scoreThreshold={scoreThreshold}
            maxRecall={maxRecall}
            onChunkingModeChange={handleChunkingModeSelect}
            onMaxChunkSizeChange={setMaxChunkSize}
            onMetadataExtractChange={setMetadataExtract}
            onExcelHeaderConcatChange={setExcelHeaderConcat}
            onMultiTurnRewriteChange={setMultiTurnRewrite}
            onRerankModelChange={setRerankModel}
            onScoreThresholdChange={setScoreThreshold}
            onMaxRecallChange={setMaxRecall}
          />
        )}

        <div className="sticky bottom-0 z-10 -mx-4 mt-8 flex items-center justify-between border-t bg-background/95 px-4 py-4 backdrop-blur-sm sm:static sm:mx-0 sm:bg-transparent sm:px-0 sm:pt-6 sm:backdrop-blur-none">
          <div>
            {step > 1 && !createdDatasetId && (
              <Button variant="outline" onClick={() => setStep((currentStep) => currentStep - 1)}>
                {t("knowledge.create.previous")}
              </Button>
            )}
          </div>
          <div className="flex items-center gap-3">
            <Button variant="outline" onClick={() => navigate("/knowledge")}>
              {t("knowledge.create.cancel")}
            </Button>
            {step < 3 ? (
              <Button variant="primary" onClick={handleNextStep}>
                {t("knowledge.create.next")}
                <ArrowRight className="ml-2 h-4 w-4" />
              </Button>
            ) : (
              <Button variant="primary" onClick={handleSubmit} disabled={isSubmitting}>
                {isSubmitting ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    {t("knowledge.create.creating")}
                  </>
                ) : (
                  t("knowledge.create.confirm")
                )}
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
