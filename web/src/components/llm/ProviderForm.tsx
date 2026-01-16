/**
 * Provider Form Component
 *
 * Modal form for creating/editing LLM providers.
 */

import { useEffect } from "react";
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
import type { Provider, ProviderCreate, ProviderUpdate } from "@/api/providers";
import { getDefaultBaseUrl } from "@/api/providers";

interface ProviderFormProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  provider?: Provider | null;
  onSubmit: (data: ProviderCreate | ProviderUpdate) => Promise<void>;
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
];

export function ProviderForm({
  open,
  onOpenChange,
  provider,
  onSubmit,
  loading,
}: ProviderFormProps) {
  const isEdit = !!provider;

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
      reset({
        provider_id: provider.provider_id,
        display_name: provider.display_name,
        api_type: provider.api_type,
        base_url: provider.base_url || "",
        api_key: "",
        is_enabled: provider.is_enabled,
      });
    } else {
      reset({
        provider_id: "",
        display_name: "",
        api_type: "openai",
        base_url: getDefaultBaseUrl("openai"),
        api_key: "",
        is_enabled: true,
      });
    }
  }, [provider, reset]);

  // Update base URL when API type changes (only for new providers)
  useEffect(() => {
    if (!isEdit) {
      setValue("base_url", getDefaultBaseUrl(apiType));
    }
  }, [apiType, isEdit, setValue]);

  const onFormSubmit = async (data: FormData) => {
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
            <DialogTitle>{isEdit ? "编辑厂商" : "添加厂商"}</DialogTitle>
            <DialogDescription>
              {isEdit ? "修改 LLM 厂商配置" : "添加新的 LLM 厂商配置"}
            </DialogDescription>
          </DialogHeader>

          <div className="grid gap-4 py-4">
            {/* Provider ID (only for create) */}
            {!isEdit && (
              <div className="grid gap-2">
                <Label htmlFor="provider_id">厂商 ID</Label>
                <Input
                  id="provider_id"
                  placeholder="例如: openai, anthropic, my-provider"
                  {...register("provider_id", {
                    required: "厂商 ID 为必填项",
                    pattern: {
                      value: /^[a-z0-9-]+$/,
                      message: "只允许小写字母、数字和连字符",
                    },
                  })}
                />
                {errors.provider_id && (
                  <p className="text-sm text-destructive">{errors.provider_id.message}</p>
                )}
              </div>
            )}

            {/* Display Name */}
            <div className="grid gap-2">
              <Label htmlFor="display_name">显示名称</Label>
              <Input
                id="display_name"
                placeholder="例如: OpenAI, Anthropic"
                {...register("display_name", { required: "显示名称为必填项" })}
              />
              {errors.display_name && (
                <p className="text-sm text-destructive">{errors.display_name.message}</p>
              )}
            </div>

            {/* API Type */}
            <div className="grid gap-2">
              <Label htmlFor="api_type">API 类型</Label>
              <Select
                value={apiType}
                onValueChange={(value) => setValue("api_type", value)}
              >
                <SelectTrigger>
                  <SelectValue placeholder="选择 API 类型" />
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

            {/* Base URL */}
            <div className="grid gap-2">
              <Label htmlFor="base_url">Base URL</Label>
              <Input
                id="base_url"
                placeholder="https://api.openai.com"
                {...register("base_url")}
              />
              <p className="text-xs text-muted-foreground">
                自定义 API 端点 URL，留空使用默认值
              </p>
            </div>

            {/* API Key */}
            <div className="grid gap-2">
              <Label htmlFor="api_key">
                API Key
                {isEdit && provider?.has_api_key && (
                  <span className="text-muted-foreground font-normal ml-2">
                    (留空保持不变)
                  </span>
                )}
              </Label>
              <Input
                id="api_key"
                type="password"
                placeholder={isEdit && provider?.has_api_key ? "••••••••" : "sk-..."}
                {...register("api_key")}
              />
            </div>

            {/* Is Enabled */}
            <div className="flex items-center justify-between">
              <Label htmlFor="is_enabled">启用厂商</Label>
              <Switch
                id="is_enabled"
                checked={watch("is_enabled")}
                onCheckedChange={(checked) => setValue("is_enabled", checked)}
              />
            </div>
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              取消
            </Button>
            <Button type="submit" disabled={loading}>
              {loading ? "保存中..." : "保存"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
