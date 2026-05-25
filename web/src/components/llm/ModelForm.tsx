/**
 * Model Form Component
 *
 * Modal form for creating/editing LLM models.
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
import type { LLMModel, ModelCreate, ModelUpdate, ModelAccessLevel } from "@/api/models";
import type { Provider, ProviderTemplate } from "@/api/providers";

interface ModelFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  model?: LLMModel | null;
  providers: Provider[];
  providerTemplates?: ProviderTemplate[];
  onSubmit: (data: ModelCreate | ModelUpdate) => Promise<void>;
  loading?: boolean;
}

interface FormData {
  model_id: string;
  provider_id: string;
  catalog_model_id: string;
  display_name: string;
  context_window: number;
  max_output_tokens: number;
  supports_vision: boolean;
  supports_tools: boolean;
  input_price_per_1k: number;
  output_price_per_1k: number;
  access_level: ModelAccessLevel;
  is_enabled: boolean;
  sort_order: number;
}

interface CatalogProviderOption {
  value: string;
  label: string;
  description?: string;
  provider?: Provider;
  template?: ProviderTemplate;
  registered: boolean;
  hasApiKey: boolean;
}

const CUSTOM_MODEL_VALUE = "__custom_model__";

function normalizeUrl(value?: string | null) {
  return (value || "").trim().replace(/\/+$/, "").toLowerCase();
}

function matchProviderTemplate(
  provider: Provider,
  templates: ProviderTemplate[]
): ProviderTemplate | undefined {
  const providerId = provider.provider_id.toLowerCase();
  const baseUrl = normalizeUrl(provider.base_url);

  return (
    templates.find((template) => template.default_provider_id === providerId) ||
    templates.find(
      (template) =>
        normalizeUrl(template.default_base_url) !== "" &&
        normalizeUrl(template.default_base_url) === baseUrl
    )
  );
}

function applyCatalogModel(
  catalogModel: ProviderTemplate["default_models"][number],
  setValue: ReturnType<typeof useForm<FormData>>["setValue"]
) {
  setValue("model_id", catalogModel.model_id);
  setValue("catalog_model_id", catalogModel.model_id);
  setValue("display_name", catalogModel.display_name);
  setValue("context_window", catalogModel.context_window);
  setValue("max_output_tokens", catalogModel.max_output_tokens);
  setValue("supports_vision", catalogModel.supports_vision);
  setValue("supports_tools", catalogModel.supports_tools);
  setValue("input_price_per_1k", catalogModel.input_price_per_1k);
  setValue("output_price_per_1k", catalogModel.output_price_per_1k);
  setValue("access_level", catalogModel.access_level as ModelAccessLevel);
  setValue("sort_order", catalogModel.sort_order);
}

export function ModelForm({
  open,
  onOpenChange,
  model,
  providers,
  providerTemplates = [],
  onSubmit,
  loading,
}: ModelFormProps) {
  const { t } = useTranslation();
  const isEdit = !!model;
  const [advancedCatalogOverride, setAdvancedCatalogOverride] = useState(false);

  const ACCESS_LEVELS: { value: ModelAccessLevel; label: string }[] = [
    { value: "public", label: t("llm.model.accessLevels.public") },
    { value: "premium", label: t("llm.model.accessLevels.premium") },
    { value: "admin", label: t("llm.model.accessLevels.admin") },
  ];

  const {
    register,
    handleSubmit,
    reset,
    watch,
    setValue,
    formState: { errors },
  } = useForm<FormData>({
    defaultValues: {
      model_id: "",
      provider_id: "",
      catalog_model_id: CUSTOM_MODEL_VALUE,
      display_name: "",
      context_window: 128000,
      max_output_tokens: 4096,
      supports_vision: false,
      supports_tools: true,
      input_price_per_1k: 0,
      output_price_per_1k: 0,
      access_level: "public",
      is_enabled: true,
      sort_order: 0,
    },
    mode: "onBlur", // Validate on blur for better UX
  });

  const providerId = watch("provider_id");
  const modelId = watch("model_id");
  const catalogModelId = watch("catalog_model_id");
  const accessLevel = watch("access_level");
  const providerOptions = useMemo<CatalogProviderOption[]>(() => {
    const registeredOptions = providers.map((provider) => {
      const template = matchProviderTemplate(provider, providerTemplates);
      return {
        value: provider.provider_id,
        label: provider.display_name,
        description: provider.base_url || undefined,
        provider,
        template,
        registered: true,
        hasApiKey: provider.has_api_key,
      };
    });
    const coveredTemplateIds = new Set(
      registeredOptions
        .map((option) => option.template?.template_id)
        .filter(Boolean) as string[]
    );
    const registeredProviderIds = new Set(
      registeredOptions.map((option) => option.value)
    );
    const templateOptions = providerTemplates
      .filter((template) => {
        if (!template.default_provider_id) return false;
        if (template.advanced) return false;
        if (coveredTemplateIds.has(template.template_id)) return false;
        if (registeredProviderIds.has(template.default_provider_id)) return false;
        return template.default_models.length > 0;
      })
      .map((template) => ({
        value: template.default_provider_id,
        label: template.display_name,
        description: template.default_base_url,
        template,
        registered: false,
        hasApiKey: false,
      }));

    return [...registeredOptions, ...templateOptions].sort((a, b) => {
      const aCatalog = a.template?.default_models.length ? 1 : 0;
      const bCatalog = b.template?.default_models.length ? 1 : 0;
      const aConfigured = a.registered && a.hasApiKey ? 1 : 0;
      const bConfigured = b.registered && b.hasApiKey ? 1 : 0;
      if (aConfigured !== bConfigured) return bConfigured - aConfigured;
      if (aCatalog !== bCatalog) return bCatalog - aCatalog;
      if (a.registered !== b.registered) return a.registered ? -1 : 1;
      return a.label.localeCompare(b.label);
    });
  }, [providers, providerTemplates]);
  const selectedProviderOption = providerOptions.find(
    (option) => option.value === providerId
  );
  const catalogModels = selectedProviderOption?.template?.default_models ?? [];
  const knownModelProviders = useMemo(() => {
    const normalized = (modelId || "").trim().toLowerCase();
    if (!normalized) return [];
    return providerTemplates
      .filter((template) =>
        template.default_models.some((catalogModel) => {
          return catalogModel.model_id.toLowerCase() === normalized;
        })
      )
      .map((template) => template.default_provider_id);
  }, [modelId, providerTemplates]);
  const selectedProviderCatalogMatchesModel = useMemo(() => {
    const normalized = (modelId || "").trim().toLowerCase();
    if (!normalized) return false;

    return Boolean(
      selectedProviderOption?.template?.default_models.some(
        (catalogModel) => catalogModel.model_id.toLowerCase() === normalized
      )
    );
  }, [modelId, selectedProviderOption]);
  const catalogMismatch =
    !isEdit &&
    !advancedCatalogOverride &&
    knownModelProviders.length > 0 &&
    !knownModelProviders.includes(providerId) &&
    !selectedProviderCatalogMatchesModel;

  useEffect(() => {
    if (model) {
      setAdvancedCatalogOverride(false);
      reset({
        model_id: model.model_id,
        provider_id: model.provider_id,
        catalog_model_id: CUSTOM_MODEL_VALUE,
        display_name: model.display_name,
        context_window: model.context_window,
        max_output_tokens: model.max_output_tokens,
        supports_vision: model.supports_vision,
        supports_tools: model.supports_tools,
        input_price_per_1k: model.input_price_per_1k,
        output_price_per_1k: model.output_price_per_1k,
        access_level: model.access_level,
        is_enabled: model.is_enabled,
        sort_order: model.sort_order,
      });
    } else {
      setAdvancedCatalogOverride(false);
      reset({
        model_id: "",
        provider_id: providerOptions[0]?.value || "",
        catalog_model_id:
          providerOptions[0]?.template?.default_models[0]?.model_id ||
          CUSTOM_MODEL_VALUE,
        display_name: "",
        context_window: 128000,
        max_output_tokens: 4096,
        supports_vision: false,
        supports_tools: true,
        input_price_per_1k: 0,
        output_price_per_1k: 0,
        access_level: "public",
        is_enabled: true,
        sort_order: 0,
      });
    }
  }, [model, providerOptions, reset]);

  useEffect(() => {
    if (isEdit || !modelId || !providerId) return;

    const template = selectedProviderOption?.template;
    const catalogModel = template?.default_models.find(
      (item) => item.model_id.toLowerCase() === modelId.trim().toLowerCase()
    );
    if (!catalogModel) return;

    applyCatalogModel(catalogModel, setValue);
  }, [isEdit, modelId, providerId, selectedProviderOption, setValue]);

  useEffect(() => {
    if (isEdit || !providerId || catalogModelId === CUSTOM_MODEL_VALUE) return;
    const catalogModel = catalogModels.find(
      (item) => item.model_id === catalogModelId
    );
    if (!catalogModel) return;

    applyCatalogModel(catalogModel, setValue);
  }, [catalogModelId, catalogModels, isEdit, providerId, setValue]);

  const onFormSubmit = async (data: FormData) => {
    const submitData: ModelCreate | ModelUpdate = isEdit
      ? {
          // ``model_id`` may be a rename in edit mode; the server treats
          // unchanged values as a no-op and new values as a PK UPDATE.
          model_id: data.model_id,
          display_name: data.display_name,
          context_window: data.context_window,
          max_output_tokens: data.max_output_tokens,
          supports_vision: data.supports_vision,
          supports_tools: data.supports_tools,
          input_price_per_1k: data.input_price_per_1k,
          output_price_per_1k: data.output_price_per_1k,
          access_level: data.access_level,
          is_enabled: data.is_enabled,
          sort_order: data.sort_order,
        }
      : {
          model_id: data.model_id,
          provider_id: data.provider_id,
          display_name: data.display_name,
          context_window: data.context_window,
          max_output_tokens: data.max_output_tokens,
          supports_vision: data.supports_vision,
          supports_tools: data.supports_tools,
          input_price_per_1k: data.input_price_per_1k,
          output_price_per_1k: data.output_price_per_1k,
          access_level: data.access_level,
          is_enabled: data.is_enabled,
          sort_order: data.sort_order,
        };

    await onSubmit(submitData);
  };

  const handleProviderChange = (value: string) => {
    setValue("provider_id", value);
    const option = providerOptions.find((item) => item.value === value);
    const firstCatalogModel = option?.template?.default_models[0];
    if (firstCatalogModel) {
      applyCatalogModel(firstCatalogModel, setValue);
    } else {
      setValue("catalog_model_id", CUSTOM_MODEL_VALUE);
    }
  };

  const handleCatalogModelChange = (value: string) => {
    setValue("catalog_model_id", value);
    if (value === CUSTOM_MODEL_VALUE) return;

    const catalogModel = catalogModels.find((item) => item.model_id === value);
    if (catalogModel) {
      applyCatalogModel(catalogModel, setValue);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[600px] max-h-[90vh] overflow-y-auto">
        <form onSubmit={handleSubmit(onFormSubmit)}>
          <DialogHeader>
            <DialogTitle>{isEdit ? t("llm.model.editTitle") : t("llm.model.addTitle")}</DialogTitle>
            <DialogDescription>
              {isEdit ? t("llm.model.editDescription") : t("llm.model.addDescription")}
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-6 py-4">
            {/* =========================================================
                SECTION: Identity
                Primary keys + human-facing name. ``model_id`` drives the
                provider API call; ``provider_id`` is immutable post-create
                to avoid re-keying usage records.
                ========================================================= */}
            <section className="grid gap-4">
              <h3 className="text-sm font-semibold text-foreground">
                {t("llm.model.sections.identity", "Identity")}
              </h3>

              {/* Provider (create only — moving a model between providers
                  is a much bigger operation and out of scope for this form). */}
              {!isEdit && (
                <div className="grid gap-2">
                  <Label htmlFor="provider_id">{t("llm.model.provider")}</Label>
                  <Select
                    value={providerId}
                    onValueChange={handleProviderChange}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder={t("llm.model.providerPlaceholder")} />
                    </SelectTrigger>
                    <SelectContent>
                      {providerOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                          {option.template?.default_models.length
                            ? ` · ${t(
                                "llm.model.catalogModelCount",
                                "{{count}} catalog models",
                                { count: option.template.default_models.length }
                              )}`
                            : ""}
                          {!option.registered
                            ? ` · ${t("llm.model.templateOnly", "template")}`
                            : ""}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-muted-foreground">
                    {selectedProviderOption?.registered
                      ? selectedProviderOption.description ||
                        t("llm.model.registeredProviderHint", "Registered provider")
                      : t(
                          "llm.model.templateProviderHint",
                          "Template entry. Save the provider credentials before using it in runtime."
                        )}
                  </p>
                </div>
              )}

              {!isEdit && catalogModels.length > 0 && (
                <div className="grid gap-2">
                  <Label htmlFor="catalog_model_id">
                    {t("llm.model.catalogModel", "Catalog model")}
                  </Label>
                  <Select
                    value={catalogModelId}
                    onValueChange={handleCatalogModelChange}
                  >
                    <SelectTrigger>
                      <SelectValue
                        placeholder={t(
                          "llm.model.catalogModelPlaceholder",
                          "Select a supported model"
                        )}
                      />
                    </SelectTrigger>
                    <SelectContent>
                      {catalogModels.map((catalogModel) => (
                        <SelectItem
                          key={catalogModel.model_id}
                          value={catalogModel.model_id}
                        >
                          {catalogModel.display_name}
                        </SelectItem>
                      ))}
                      <SelectItem value={CUSTOM_MODEL_VALUE}>
                        {t("llm.model.customModel", "Custom model ID")}
                      </SelectItem>
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-muted-foreground">
                    {t(
                      "llm.model.catalogModelHint",
                      "Choosing a catalog model fills capability, context, and pricing defaults."
                    )}
                  </p>
                </div>
              )}

              {/* Model ID — editable in both create and edit modes. Renaming
                  (e.g. ``gemini-3-flash-preview`` → ``gemini-3.1-flash``) is
                  supported server-side via PK UPDATE so operators don't
                  have to delete+recreate when the provider renames a model. */}
              <div className="grid gap-2">
                <Label htmlFor="model_id">{t("llm.model.modelId")}</Label>
                <Input
                  id="model_id"
                  placeholder={t("llm.model.modelIdPlaceholder")}
                  {...register("model_id", {
                    required: t("llm.model.modelIdRequired"),
                    onChange: () => {
                      if (!isEdit) {
                        setValue("catalog_model_id", CUSTOM_MODEL_VALUE);
                      }
                    },
                  })}
                />
                {errors.model_id && (
                  <p className="text-sm text-destructive">{errors.model_id.message}</p>
                )}
                <p className="text-xs text-muted-foreground">
                  {isEdit
                    ? t(
                        "llm.model.modelIdEditHint",
                        "This is the identifier sent to the provider's API. Renaming it updates the primary key; prior usage records keep their original id for audit.",
                      )
                    : t("llm.model.modelIdHint")}
                </p>
              </div>

              {catalogMismatch && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
                  {t(
                    "llm.model.catalogMismatch",
                    "This model belongs to a known provider catalog. Switch to the matching provider or enable advanced override."
                  )}{" "}
                  <button
                    type="button"
                    className="font-semibold underline"
                    onClick={() => setAdvancedCatalogOverride(true)}
                  >
                    {t("llm.model.enableAdvancedOverride", "Enable advanced override")}
                  </button>
                </div>
              )}

              {/* Display Name */}
              <div className="grid gap-2">
                <Label htmlFor="display_name">{t("llm.model.displayName")}</Label>
                <Input
                  id="display_name"
                  placeholder={t("llm.model.displayNamePlaceholder")}
                  {...register("display_name", { required: t("llm.model.displayNameRequired") })}
                />
                {errors.display_name && (
                  <p className="text-sm text-destructive">{errors.display_name.message}</p>
                )}
              </div>
            </section>

            {/* =========================================================
                SECTION: Capabilities
                Context budget + feature switches. Note: ``supports_native_search``
                and ``native_search_config`` are intentionally NOT exposed
                here — they are DERIVED (populated at runtime from the
                ``NATIVE_SEARCH_CAPABLE`` map in ``model_registry.py`` based
                on the (provider, model_id) pair). Flipping a DB boolean
                without corresponding provider-body wiring in ``_build_*_body``
                would produce 400 errors, so the map is the single source
                of truth. To enable native search for a new model, a code
                change is required — add the (provider, model_id) entry to
                ``NATIVE_SEARCH_CAPABLE`` and the matching request-body
                merge in the provider-specific ``_build_*_body`` function.
                ========================================================= */}
            <section className="grid gap-4">
              <h3 className="text-sm font-semibold text-foreground">
                {t("llm.model.sections.capabilities", "Capabilities")}
              </h3>

              <div className="grid grid-cols-2 gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="context_window">
                    {t("llm.model.contextWindow")} <span className="text-destructive">*</span>
                  </Label>
                  <Input
                    id="context_window"
                    type="number"
                    {...register("context_window", {
                      valueAsNumber: true,
                      required: t("llm.model.validation.contextWindowRequired"),
                      min: { value: 1, message: t("llm.model.validation.contextWindowMin") },
                      max: { value: 10000000, message: t("llm.model.validation.contextWindowMax") },
                    })}
                  />
                  {errors.context_window && (
                    <p className="text-sm text-destructive">{errors.context_window.message}</p>
                  )}
                  <p className="text-xs text-muted-foreground">{t("llm.model.tokenCount")}</p>
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="max_output_tokens">{t("llm.model.maxOutputTokens")}</Label>
                  <Input
                    id="max_output_tokens"
                    type="number"
                    {...register("max_output_tokens", {
                      valueAsNumber: true,
                      min: { value: 1, message: t("llm.model.validation.maxOutputTokensMin") },
                    })}
                  />
                  {errors.max_output_tokens && (
                    <p className="text-sm text-destructive">{errors.max_output_tokens.message}</p>
                  )}
                  <p className="text-xs text-muted-foreground">{t("llm.model.tokenCount")}</p>
                </div>
              </div>

              <div className="flex items-center justify-between">
                <div>
                  <Label htmlFor="supports_vision">{t("llm.model.supportsVision")}</Label>
                  <p className="text-xs text-muted-foreground">
                    {t("llm.model.supportsVisionHint")}
                  </p>
                </div>
                <Switch
                  id="supports_vision"
                  checked={watch("supports_vision")}
                  onCheckedChange={(checked) => setValue("supports_vision", checked)}
                />
              </div>

              <div className="flex items-center justify-between">
                <div>
                  <Label htmlFor="supports_tools">{t("llm.model.supportsTools")}</Label>
                  <p className="text-xs text-muted-foreground">
                    {t("llm.model.supportsToolsHint")}
                  </p>
                </div>
                <Switch
                  id="supports_tools"
                  checked={watch("supports_tools")}
                  onCheckedChange={(checked) => setValue("supports_tools", checked)}
                />
              </div>

              <p className="text-xs text-muted-foreground italic">
                {t(
                  "llm.model.nativeSearchNote",
                  "Native web search support is derived from the (provider, model_id) pair in code (see NATIVE_SEARCH_CAPABLE). Adding support for a new model requires a code change, not a UI toggle.",
                )}
              </p>
            </section>

            {/* =========================================================
                SECTION: Pricing
                USD per 1K tokens. Values are synced to ``model_pricing``
                on save so UsageRecorder's cost calculations pick them up.
                ========================================================= */}
            <section className="grid gap-4">
              <h3 className="text-sm font-semibold text-foreground">
                {t("llm.model.sections.pricing", "Pricing")}
              </h3>
              <div className="grid grid-cols-2 gap-4">
                <div className="grid gap-2">
                  <Label htmlFor="input_price_per_1k">{t("llm.model.inputPrice")}</Label>
                  <Input
                    id="input_price_per_1k"
                    type="number"
                    step="0.000001"
                    {...register("input_price_per_1k", {
                      valueAsNumber: true,
                      min: { value: 0, message: t("llm.model.validation.priceMin") },
                    })}
                  />
                  {errors.input_price_per_1k && (
                    <p className="text-sm text-destructive">{errors.input_price_per_1k.message}</p>
                  )}
                  <p className="text-xs text-muted-foreground">{t("llm.model.priceUnit")}</p>
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="output_price_per_1k">{t("llm.model.outputPrice")}</Label>
                  <Input
                    id="output_price_per_1k"
                    type="number"
                    step="0.000001"
                    {...register("output_price_per_1k", {
                      valueAsNumber: true,
                      min: { value: 0, message: t("llm.model.validation.priceMin") },
                    })}
                  />
                  {errors.output_price_per_1k && (
                    <p className="text-sm text-destructive">{errors.output_price_per_1k.message}</p>
                  )}
                  <p className="text-xs text-muted-foreground">{t("llm.model.priceUnit")}</p>
                </div>
              </div>
            </section>

            {/* =========================================================
                SECTION: Access & Display
                Who can see/use the model + UI sort order + enabled flag.
                ========================================================= */}
            <section className="grid gap-4">
              <h3 className="text-sm font-semibold text-foreground">
                {t("llm.model.sections.accessDisplay", "Access & Display")}
              </h3>

              <div className="grid gap-2">
                <Label htmlFor="access_level">{t("llm.model.accessLevel")}</Label>
                <Select
                  value={accessLevel}
                  onValueChange={(value) => setValue("access_level", value as ModelAccessLevel)}
                >
                  <SelectTrigger>
                    <SelectValue placeholder={t("llm.model.accessLevelPlaceholder")} />
                  </SelectTrigger>
                  <SelectContent>
                    {ACCESS_LEVELS.map((level) => (
                      <SelectItem key={level.value} value={level.value}>
                        {level.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="grid gap-2">
                <Label htmlFor="sort_order">{t("llm.model.sortOrder")}</Label>
                <Input
                  id="sort_order"
                  type="number"
                  {...register("sort_order", { valueAsNumber: true })}
                />
                <p className="text-xs text-muted-foreground">{t("llm.model.sortOrderHint")}</p>
              </div>

              <div className="flex items-center justify-between">
                <div>
                  <Label htmlFor="is_enabled">{t("llm.model.enableModel")}</Label>
                  <p className="text-xs text-muted-foreground">
                    {t("llm.model.enableModelHint")}
                  </p>
                </div>
                <Switch
                  id="is_enabled"
                  checked={watch("is_enabled")}
                  onCheckedChange={(checked) => setValue("is_enabled", checked)}
                />
              </div>
            </section>
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={loading || catalogMismatch}>
              {loading ? t("common.loading") : t("common.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
