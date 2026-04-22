/**
 * Model Form Component
 *
 * Modal form for creating/editing LLM models.
 */

import { useEffect } from "react";
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
import type { Provider } from "@/api/providers";

interface ModelFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  model?: LLMModel | null;
  providers: Provider[];
  onSubmit: (data: ModelCreate | ModelUpdate) => Promise<void>;
  loading?: boolean;
}

interface FormData {
  model_id: string;
  provider_id: string;
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

export function ModelForm({
  open,
  onOpenChange,
  model,
  providers,
  onSubmit,
  loading,
}: ModelFormProps) {
  const { t } = useTranslation();
  const isEdit = !!model;

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
  const accessLevel = watch("access_level");

  useEffect(() => {
    if (model) {
      reset({
        model_id: model.model_id,
        provider_id: model.provider_id,
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
      reset({
        model_id: "",
        provider_id: providers[0]?.provider_id || "",
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
  }, [model, providers, reset]);

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

              {/* Provider (create only — moving a model between providers
                  is a much bigger operation and out of scope for this form). */}
              {!isEdit && (
                <div className="grid gap-2">
                  <Label htmlFor="provider_id">{t("llm.model.provider")}</Label>
                  <Select
                    value={providerId}
                    onValueChange={(value) => setValue("provider_id", value)}
                  >
                    <SelectTrigger>
                      <SelectValue placeholder={t("llm.model.providerPlaceholder")} />
                    </SelectTrigger>
                    <SelectContent>
                      {providers.map((p) => (
                        <SelectItem key={p.provider_id} value={p.provider_id}>
                          {p.display_name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
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
            <Button type="submit" disabled={loading}>
              {loading ? t("common.loading") : t("common.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
