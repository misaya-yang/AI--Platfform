/**
 * Provider Form Component
 *
 * Modal form for creating/editing LLM providers.
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useForm } from "react-hook-form";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { cn } from "@/lib/utils";
import type {
  Provider,
  ProviderCreate,
  ProviderFromTemplateCreate,
  ProviderTemplate,
  ProviderUpdate,
} from "@/api/providers";
import { getDefaultBaseUrl } from "@/api/providers";

interface ProviderFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  provider?: Provider | null;
  templates?: ProviderTemplate[];
  onSubmit: (data: ProviderCreate | ProviderFromTemplateCreate | ProviderUpdate) => Promise<void>;
  loading?: boolean;
}

interface FormData {
  provider_id: string;
  display_name: string;
  api_type: string;
  base_url: string;
  api_key: string;
  is_enabled: boolean;
}

const API_TYPES = [
  { value: "openai", label: "OpenAI Compatible" },
  { value: "anthropic", label: "Anthropic" },
  { value: "google", label: "Google Gemini" },
  { value: "google-vertex", label: "Google Vertex AI" },
];

// Per-api-type hints for the API key field. Vertex Express Mode keys have
// a distinct format (``AQ.xxx``) and land on a different host, so surface
// that in the placeholder to reduce copy/paste-the-wrong-key incidents.
const API_KEY_PLACEHOLDERS: Record<string, string> = {
  openai: "sk-...",
  anthropic: "sk-ant-...",
  google: "AIzaSy...",
  "google-vertex": "AQ.xxx  (Vertex Express Mode key)",
};

export function ProviderForm({
  open,
  onOpenChange,
  provider,
  templates = [],
  onSubmit,
  loading,
}: ProviderFormProps) {
  const { t } = useTranslation();
  const isEdit = !!provider;
  const guidedTemplates = useMemo(
    () => templates.filter((template) => template.template_id !== "custom-openai-compatible"),
    [templates]
  );
  const [advancedMode, setAdvancedMode] = useState(false);
  const [selectedTemplateId, setSelectedTemplateId] = useState<string>("");
  const selectedTemplate = templates.find(
    (template) => template.template_id === selectedTemplateId
  );

  const {
    register,
    handleSubmit,
    reset,
    watch,
    setValue,
    formState: { errors },
  } = useForm<FormData>({
    defaultValues: {
      provider_id: "",
      display_name: "",
      api_type: "openai",
      base_url: "",
      api_key: "",
      is_enabled: true,
    },
  });

  const apiType = watch("api_type");

  useEffect(() => {
    if (provider) {
      setAdvancedMode(true);
      reset({
        provider_id: provider.provider_id,
        display_name: provider.display_name,
        api_type: provider.api_type,
        base_url: provider.base_url || "",
        api_key: "",
        is_enabled: provider.is_enabled,
      });
    } else {
      setAdvancedMode(false);
      setSelectedTemplateId(guidedTemplates[0]?.template_id || "");
      reset({
        provider_id: "",
        display_name: "",
        api_type: "openai",
        base_url: getDefaultBaseUrl("openai"),
        api_key: "",
        is_enabled: true,
      });
    }
  }, [guidedTemplates, provider, reset]);

  // Update base URL when API type changes (only for new providers)
  useEffect(() => {
    if (!isEdit) {
      setValue("base_url", getDefaultBaseUrl(apiType));
    }
  }, [apiType, isEdit, setValue]);

  useEffect(() => {
    if (!isEdit && selectedTemplate && !advancedMode) {
      setValue("api_type", selectedTemplate.api_type);
      setValue("base_url", selectedTemplate.default_base_url || "");
      setValue("provider_id", selectedTemplate.default_provider_id);
      setValue("display_name", selectedTemplate.display_name);
    }
  }, [advancedMode, isEdit, selectedTemplate, setValue]);

  const onFormSubmit = async (data: FormData) => {
    if (!isEdit && selectedTemplate && !advancedMode) {
      const submitData: ProviderFromTemplateCreate = {
        template_id: selectedTemplate.template_id,
        api_key: data.api_key || undefined,
        is_enabled: data.is_enabled,
      };
      await onSubmit(submitData);
      return;
    }

    const submitData: ProviderCreate | ProviderUpdate = isEdit
      ? {
          display_name: data.display_name,
          api_type: data.api_type,
          base_url: data.base_url || undefined,
          api_key: data.api_key || undefined,
          is_enabled: data.is_enabled,
        }
      : {
          provider_id: data.provider_id,
          display_name: data.display_name,
          api_type: data.api_type,
          base_url: data.base_url || undefined,
          api_key: data.api_key || undefined,
          is_enabled: data.is_enabled,
        };

    await onSubmit(submitData);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[500px]">
        <form onSubmit={handleSubmit(onFormSubmit)}>
          <DialogHeader>
            <DialogTitle>{isEdit ? t("llm.provider.editTitle") : t("llm.provider.addTitle")}</DialogTitle>
            <DialogDescription>
              {isEdit ? t("llm.provider.editDescription") : t("llm.provider.addDescription")}
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4 py-4">
            {!isEdit && guidedTemplates.length > 0 && (
              <div className="grid gap-3">
                <div className="flex items-center justify-between gap-3">
                  <Label>{t("llm.provider.template", "Provider template")}</Label>
                  <Button
                    type="button"
                    variant={advancedMode ? "default" : "outline"}
                    size="sm"
                    onClick={() => setAdvancedMode((value) => !value)}
                  >
                    {advancedMode
                      ? t("llm.provider.guidedMode", "Guided")
                      : t("llm.provider.advancedMode", "Advanced")}
                  </Button>
                </div>
                {!advancedMode && (
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                    {guidedTemplates.map((template) => (
                      <button
                        key={template.template_id}
                        type="button"
                        onClick={() => setSelectedTemplateId(template.template_id)}
                        className={cn(
                          "rounded-lg border px-3 py-2 text-left transition-colors",
                          selectedTemplateId === template.template_id
                            ? "border-primary bg-primary/5"
                            : "border-border hover:border-primary/50"
                        )}
                      >
                        <span className="block text-sm font-semibold">
                          {template.display_name}
                        </span>
                        <span className="block text-xs text-muted-foreground truncate">
                          {template.discovery_strategy}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* Provider ID (only for create) */}
            {!isEdit && advancedMode && (
              <div className="grid gap-2">
                <Label htmlFor="provider_id">{t("llm.provider.providerId")}</Label>
                <Input
                  id="provider_id"
                  placeholder={t("llm.provider.providerIdPlaceholder")}
                  {...register("provider_id", {
                    required: t("llm.provider.providerIdRequired"),
                    pattern: {
                      value: /^[a-z0-9-]+$/,
                      message: t("llm.provider.providerIdPattern"),
                    },
                  })}
                />
                {errors.provider_id && (
                  <p className="text-sm text-destructive">{errors.provider_id.message}</p>
                )}
              </div>
            )}

            {/* Display Name */}
            {(isEdit || advancedMode) && (
              <div className="grid gap-2">
              <Label htmlFor="display_name">{t("llm.provider.displayName")}</Label>
              <Input
                id="display_name"
                placeholder={t("llm.provider.displayNamePlaceholder")}
                {...register("display_name", { required: t("llm.provider.displayNameRequired") })}
              />
              {errors.display_name && (
                <p className="text-sm text-destructive">{errors.display_name.message}</p>
              )}
            </div>
            )}

            {/* API Type */}
            {(isEdit || advancedMode) && (
              <div className="grid gap-2">
              <Label htmlFor="api_type">{t("llm.provider.apiType")}</Label>
              <Select
                value={apiType}
                onValueChange={(value) => setValue("api_type", value)}
              >
                <SelectTrigger>
                  <SelectValue placeholder={t("llm.provider.apiTypePlaceholder")} />
                </SelectTrigger>
                <SelectContent>
                  {API_TYPES.map((type) => (
                    <SelectItem key={type.value} value={type.value}>
                      {type.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            )}

            {/* Base URL */}
            {(isEdit || advancedMode) && (
              <div className="grid gap-2">
              <Label htmlFor="base_url">{t("llm.provider.baseUrl")}</Label>
              <Input
                id="base_url"
                placeholder={t("llm.provider.baseUrlPlaceholder")}
                {...register("base_url")}
              />
              <p className="text-xs text-muted-foreground">
                {t("llm.provider.baseUrlHint")}
              </p>
            </div>
            )}

            {/* API Key */}
            <div className="grid gap-2">
              <Label htmlFor="api_key">
                {t("llm.provider.apiKey")}
                {isEdit && provider?.has_api_key && (
                  <span className="text-muted-foreground font-normal ml-2">
                    ({t("llm.provider.apiKeyHint")})
                  </span>
                )}
              </Label>
              <Input
                id="api_key"
                type="password"
                placeholder={
                  isEdit && provider?.has_api_key
                    ? "••••••••"
                    : API_KEY_PLACEHOLDERS[apiType] || "sk-..."
                }
                {...register("api_key")}
              />
              {apiType === "google-vertex" && (
                <p className="text-xs text-muted-foreground">
                  Paste an Express Mode API key (format <code>AQ.xxx</code>).
                  Vertex Express Mode works without OAuth / project / location
                  setup.
                </p>
              )}
            </div>

            {/* Is Enabled */}
            <div className="flex items-center justify-between">
              <Label htmlFor="is_enabled">{t("llm.provider.enableProvider")}</Label>
              <Switch
                id="is_enabled"
                checked={watch("is_enabled")}
                onCheckedChange={(checked) => setValue("is_enabled", checked)}
              />
            </div>
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={loading}>
              {loading ? t("common.loading") : t("common.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
