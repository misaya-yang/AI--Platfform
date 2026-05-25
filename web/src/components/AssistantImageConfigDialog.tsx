import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { Image as ImageIcon } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import {
  getImageConfig,
  updateImageConfig,
  type ImageModelOverrideConfig,
} from "@/api/assistant";
import * as providersApi from "@/api/providers";
import * as modelsApi from "@/api/models";

export function AssistantImageConfigDialog({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [form, setForm] = useState<ImageModelOverrideConfig>({
    enabled: false,
  });
  const [error, setError] = useState<string | null>(null);

  const configQuery = useQuery({
    queryKey: ["assistant-image-config"],
    queryFn: getImageConfig,
    enabled: open,
  });

  const providersQuery = useQuery({
    queryKey: providersApi.providerQueryKeys.all,
    queryFn: () => providersApi.listProviders(true),
    enabled: open,
  });

  const selectedProviderId = form.provider_id || "";
  const modelsQuery = useQuery({
    queryKey: modelsApi.modelQueryKeys.byProvider(
      selectedProviderId,
      true,
      "image",
    ),
    queryFn: () =>
      modelsApi.listModels(selectedProviderId, true, { model_type: "image" }),
    enabled: open && Boolean(selectedProviderId),
  });

  const providers = useMemo(
    () => (providersQuery.data ?? []).filter((provider) => provider.is_enabled),
    [providersQuery.data],
  );
  const models = useMemo(
    () => (modelsQuery.data ?? []).filter((model) => model.is_enabled),
    [modelsQuery.data],
  );
  const selectedProvider = providers.find(
    (provider) => provider.provider_id === form.provider_id,
  );
  const selectedModel = models.find(
    (model) => model.model_id === form.model_id,
  );

  /* eslint-disable react-hooks/set-state-in-effect -- Intentional: form initialization from remote config */
  useEffect(() => {
    if (!configQuery.data) return;
    setForm(configQuery.data.image_model_override || { enabled: false });
    setError(null);
  }, [configQuery.data]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const updateMutation = useMutation({
    mutationFn: (image_model_override: ImageModelOverrideConfig) =>
      updateImageConfig({ image_model_override }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["assistant-image-config"] });
      onOpenChange(false);
    },
    onError: (err) => {
      setError(err instanceof Error ? err.message : t("common.error", "Error"));
    },
  });

  const invalid =
    form.enabled &&
    (!form.provider_id ||
      !form.model_id ||
      !selectedProvider ||
      !selectedProvider.has_api_key ||
      !selectedModel);

  const handleProviderChange = (providerId: string) => {
    setForm((current) => ({
      ...current,
      provider_id: providerId,
      model_id:
        current.provider_id === providerId ? current.model_id : undefined,
    }));
  };

  const handleSave = () => {
    setError(null);
    if (invalid) {
      setError(t("services.configDialog.model.overrideInvalid"));
      return;
    }
    updateMutation.mutate({
      enabled: form.enabled,
      provider_id: form.provider_id,
      model_id: form.model_id,
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="w-[95vw] max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ImageIcon className="h-4 w-4" />
            <span>
              {t("assistant.imageConfig.title", "Image generation API")}
            </span>
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4 pt-2">
          <div className="flex items-center justify-between gap-3">
            <div className="space-y-1">
              <Label>
                {t("assistant.imageConfig.override", "Image model override")}
              </Label>
              <p className="text-xs text-muted-foreground">
                {t(
                  "assistant.imageConfig.overrideHint",
                  "Gateway resolves the provider key internally.",
                )}
              </p>
            </div>
            <Switch
              checked={form.enabled}
              onCheckedChange={(checked) =>
                setForm({ ...form, enabled: checked })
              }
            />
          </div>

          <Separator />

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label>{t("services.configDialog.model.provider")}</Label>
              <Select
                value={form.provider_id || ""}
                onValueChange={handleProviderChange}
                disabled={!form.enabled || providersQuery.isLoading}
              >
                <SelectTrigger>
                  <SelectValue
                    placeholder={t(
                      "services.configDialog.model.providerPlaceholder",
                    )}
                  />
                </SelectTrigger>
                <SelectContent>
                  {providers.map((provider) => (
                    <SelectItem
                      key={provider.provider_id}
                      value={provider.provider_id}
                    >
                      {provider.display_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="space-y-2">
              <Label>{t("services.configDialog.model.model")}</Label>
              <Select
                value={form.model_id || ""}
                onValueChange={(modelId) =>
                  setForm({ ...form, model_id: modelId })
                }
                disabled={
                  !form.enabled || !form.provider_id || modelsQuery.isLoading
                }
              >
                <SelectTrigger>
                  <SelectValue
                    placeholder={t(
                      "services.configDialog.model.modelPlaceholder",
                    )}
                  />
                </SelectTrigger>
                <SelectContent>
                  {models.map((model) => (
                    <SelectItem key={model.model_id} value={model.model_id}>
                      {model.display_name || model.model_id}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2 text-xs">
            <Badge
              variant={
                selectedProvider?.is_enabled && selectedProvider?.has_api_key
                  ? "secondary"
                  : "destructive"
              }
            >
              {selectedProvider
                ? selectedProvider.has_api_key
                  ? t("services.configDialog.model.keyConfigured")
                  : t("services.configDialog.model.keyMissing")
                : t("services.configDialog.model.providerRequired")}
            </Badge>
            <Badge
              variant={
                selectedModel || !form.model_id ? "secondary" : "destructive"
              }
            >
              {selectedModel
                ? t("services.configDialog.model.modelEnabled")
                : t("services.configDialog.model.modelRequired")}
            </Badge>
          </div>

          {error && <div className="text-sm text-destructive">{error}</div>}

          <div className="flex justify-end gap-2 pt-2">
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              onClick={handleSave}
              disabled={updateMutation.isPending || invalid}
            >
              {updateMutation.isPending ? t("common.saving") : t("common.save")}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
