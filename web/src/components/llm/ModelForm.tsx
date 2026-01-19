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

          <div className="grid gap-4 py-4">
            {/* Model ID & Provider (only for create) */}
            {!isEdit && (
              <>
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
                    {t("llm.model.modelIdHint")}
                  </p>
                </div>

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
              </>
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

            {/* Context Window & Max Output */}
            <div className="grid grid-cols-2 gap-4">
              <div className="grid gap-2">
                <Label htmlFor="context_window">{t("llm.model.contextWindow")}</Label>
                <Input
                  id="context_window"
                  type="number"
                  {...register("context_window", { valueAsNumber: true })}
                />
                <p className="text-xs text-muted-foreground">{t("llm.model.tokenCount")}</p>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="max_output_tokens">{t("llm.model.maxOutputTokens")}</Label>
                <Input
                  id="max_output_tokens"
                  type="number"
                  {...register("max_output_tokens", { valueAsNumber: true })}
                />
                <p className="text-xs text-muted-foreground">{t("llm.model.tokenCount")}</p>
              </div>
            </div>

            {/* Pricing */}
            <div className="grid grid-cols-2 gap-4">
              <div className="grid gap-2">
                <Label htmlFor="input_price_per_1k">{t("llm.model.inputPrice")}</Label>
                <Input
                  id="input_price_per_1k"
                  type="number"
                  step="0.000001"
                  {...register("input_price_per_1k", { valueAsNumber: true })}
                />
                <p className="text-xs text-muted-foreground">{t("llm.model.priceUnit")}</p>
              </div>
              <div className="grid gap-2">
                <Label htmlFor="output_price_per_1k">{t("llm.model.outputPrice")}</Label>
                <Input
                  id="output_price_per_1k"
                  type="number"
                  step="0.000001"
                  {...register("output_price_per_1k", { valueAsNumber: true })}
                />
                <p className="text-xs text-muted-foreground">{t("llm.model.priceUnit")}</p>
              </div>
            </div>

            {/* Access Level */}
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

            {/* Sort Order */}
            <div className="grid gap-2">
              <Label htmlFor="sort_order">{t("llm.model.sortOrder")}</Label>
              <Input
                id="sort_order"
                type="number"
                {...register("sort_order", { valueAsNumber: true })}
              />
              <p className="text-xs text-muted-foreground">{t("llm.model.sortOrderHint")}</p>
            </div>

            {/* Feature Switches */}
            <div className="space-y-4 pt-2">
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
