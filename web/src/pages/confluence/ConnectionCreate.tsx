/**
 * Create Confluence Connection Page
 *
 * Step-by-step wizard for creating a new Confluence connection.
 * Clean form design with validation and visual feedback.
 */

import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Cloud,
  Key,
  User,
  Globe,
  CheckCircle,
  AlertCircle,
  Loader2,
  Eye,
  EyeOff,
  Zap,
  HelpCircle,
  Clock,
  RefreshCcw,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

import { createConnection } from "@/api/confluence";
import type { ConfluenceConnectionCreateRequest } from "@/types/confluence";

// ============================================================
// Form Field Component
// ============================================================

interface FormFieldProps {
  label: string;
  required?: boolean;
  hint?: string;
  error?: string;
  children: React.ReactNode;
}

function FormField({ label, required, hint, error, children }: FormFieldProps) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Label className="text-sm font-medium text-foreground">
          {label}
          {required && <span className="text-rose-500 ml-0.5">*</span>}
        </Label>
        {hint && (
          <TooltipProvider>
            <Tooltip>
              <TooltipTrigger>
                <HelpCircle className="h-3.5 w-3.5 text-muted-foreground" />
              </TooltipTrigger>
              <TooltipContent side="right" className="max-w-xs">
                <p className="text-xs">{hint}</p>
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
      </div>
      {children}
      {error && (
        <p className="text-xs text-rose-500 flex items-center gap-1">
          <AlertCircle className="h-3 w-3" />
          {error}
        </p>
      )}
    </div>
  );
}

// ============================================================
// Main Component
// ============================================================

interface FormData {
  name: string;
  domain: string;
  email: string;
  api_token: string;
  sync_mode: "manual" | "polling";
  polling_interval_minutes: number;
}

interface FormErrors {
  name?: string;
  domain?: string;
  email?: string;
  api_token?: string;
}

export default function ConnectionCreatePage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useTranslation();

  const [formData, setFormData] = useState<FormData>({
    name: "",
    domain: "",
    email: "",
    api_token: "",
    sync_mode: "manual",
    polling_interval_minutes: 60,
  });

  const [errors, setErrors] = useState<FormErrors>({});
  const [showToken, setShowToken] = useState(false);
  const [testStatus, setTestStatus] = useState<"idle" | "testing" | "success" | "error">("idle");
  const [testMessage, setTestMessage] = useState("");

  // Create connection mutation
  const createMutation = useMutation({
    mutationFn: (data: ConfluenceConnectionCreateRequest) => createConnection(data),
    onSuccess: (connection) => {
      queryClient.invalidateQueries({ queryKey: ["confluence-connections"] });
      // Navigate to bind space page
      navigate(`/confluence/connections/${connection.connection_id}/bind`);
    },
  });

  // Validation
  const validateForm = (): boolean => {
    const newErrors: FormErrors = {};

    if (!formData.name.trim()) {
      newErrors.name = t("confluence.create.errors.nameRequired");
    }

    if (!formData.domain.trim()) {
      newErrors.domain = t("confluence.create.errors.domainRequired");
    } else if (!formData.domain.includes(".atlassian.net")) {
      newErrors.domain = t("confluence.create.errors.domainInvalid");
    }

    if (!formData.email.trim()) {
      newErrors.email = t("confluence.create.errors.emailRequired");
    } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(formData.email)) {
      newErrors.email = t("confluence.create.errors.emailInvalid");
    }

    if (!formData.api_token.trim()) {
      newErrors.api_token = t("confluence.create.errors.tokenRequired");
    }

    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  // Test connection
  const handleTestConnection = async () => {
    if (!validateForm()) return;

    setTestStatus("testing");
    setTestMessage("");

    try {
      // Create a temporary test by calling the backend
      const response = await fetch("/api/v1/confluence/connections/test", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${localStorage.getItem("token")}`,
        },
        body: JSON.stringify({
          domain: formData.domain,
          email: formData.email,
          api_token: formData.api_token,
        }),
      });

      const result = await response.json();

      if (response.ok && result.status === "success") {
        setTestStatus("success");
        setTestMessage(result.message || t("confluence.test.success"));
      } else {
        setTestStatus("error");
        setTestMessage(result.message || result.detail || t("confluence.test.failed"));
      }
    } catch (error) {
      setTestStatus("error");
      setTestMessage(error instanceof Error ? error.message : t("confluence.test.failed"));
    }
  };

  // Handle submit
  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!validateForm()) return;

    createMutation.mutate({
      name: formData.name.trim(),
      domain: formData.domain.trim(),
      email: formData.email.trim(),
      api_token: formData.api_token,
      sync_mode: formData.sync_mode,
      polling_interval_minutes: formData.polling_interval_minutes,
    });
  };

  // Update form field
  const updateField = <K extends keyof FormData>(field: K, value: FormData[K]) => {
    setFormData((prev) => ({ ...prev, [field]: value }));
    // Clear error on change
    if (errors[field as keyof FormErrors]) {
      setErrors((prev) => ({ ...prev, [field]: undefined }));
    }
    // Reset test status on credential change
    if (["domain", "email", "api_token"].includes(field)) {
      setTestStatus("idle");
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-background to-muted/20">
      {/* Header */}
      <div className="bg-card/80 backdrop-blur-sm border-b border-border/50 sticky top-0 z-20">
        <div className="max-w-3xl mx-auto px-6">
          <div className="flex items-center h-16 gap-4">
            <Button
              variant="ghost"
              size="icon"
              onClick={() => navigate("/confluence")}
              className="h-9 w-9"
            >
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-blue-500 to-cyan-500 flex items-center justify-center">
                <Cloud className="h-5 w-5 text-white" />
              </div>
              <div>
                <h1 className="text-lg font-semibold text-foreground">{t("confluence.create.title")}</h1>
                <p className="text-xs text-muted-foreground">{t("confluence.create.subtitle")}</p>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="max-w-3xl mx-auto px-6 py-8">
        <form onSubmit={handleSubmit} className="space-y-8">
          {/* Basic Info Card */}
          <Card className="p-6 border-border/60">
            <div className="flex items-center gap-3 mb-6">
              <div className="w-8 h-8 rounded-lg bg-blue-500/10 flex items-center justify-center">
                <Globe className="h-4 w-4 text-blue-500" />
              </div>
              <h2 className="font-semibold text-foreground">{t("confluence.create.connectionDetails")}</h2>
            </div>

            <div className="space-y-5">
              <FormField
                label={t("confluence.create.name")}
                required
                hint={t("confluence.create.nameHint")}
                error={errors.name}
              >
                <Input
                  placeholder={t("confluence.create.namePlaceholder")}
                  value={formData.name}
                  onChange={(e) => updateField("name", e.target.value)}
                  className="bg-background"
                />
              </FormField>

              <FormField
                label={t("confluence.create.domain")}
                required
                hint={t("confluence.create.domainHint")}
                error={errors.domain}
              >
                <div className="relative">
                  <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground">
                    https://
                  </span>
                  <Input
                    placeholder={t("confluence.create.domainPlaceholder")}
                    value={formData.domain}
                    onChange={(e) => updateField("domain", e.target.value)}
                    className="pl-16 bg-background"
                  />
                </div>
              </FormField>
            </div>
          </Card>

          {/* Credentials Card */}
          <Card className="p-6 border-border/60">
            <div className="flex items-center gap-3 mb-6">
              <div className="w-8 h-8 rounded-lg bg-amber-500/10 flex items-center justify-center">
                <Key className="h-4 w-4 text-amber-500" />
              </div>
              <h2 className="font-semibold text-foreground">{t("confluence.create.authentication")}</h2>
            </div>

            <div className="space-y-5">
              <FormField
                label={t("confluence.create.email")}
                required
                hint={t("confluence.create.emailHint")}
                error={errors.email}
              >
                <div className="relative">
                  <User className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    type="email"
                    placeholder={t("confluence.create.emailPlaceholder")}
                    value={formData.email}
                    onChange={(e) => updateField("email", e.target.value)}
                    className="pl-10 bg-background"
                  />
                </div>
              </FormField>

              <FormField
                label={t("confluence.create.apiToken")}
                required
                hint={t("confluence.create.apiTokenHint")}
                error={errors.api_token}
              >
                <div className="relative">
                  <Key className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                  <Input
                    type={showToken ? "text" : "password"}
                    placeholder={t("confluence.create.apiTokenPlaceholder")}
                    value={formData.api_token}
                    onChange={(e) => updateField("api_token", e.target.value)}
                    className="pl-10 pr-10 bg-background font-mono"
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    onClick={() => setShowToken(!showToken)}
                    className="absolute right-1 top-1/2 -translate-y-1/2 h-8 w-8"
                  >
                    {showToken ? (
                      <EyeOff className="h-4 w-4 text-muted-foreground" />
                    ) : (
                      <Eye className="h-4 w-4 text-muted-foreground" />
                    )}
                  </Button>
                </div>
              </FormField>

              {/* Test Connection */}
              <div className="pt-2">
                <Button
                  type="button"
                  variant="outline"
                  onClick={handleTestConnection}
                  disabled={testStatus === "testing"}
                  className="w-full"
                >
                  {testStatus === "testing" ? (
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  ) : (
                    <Zap className="h-4 w-4 mr-2" />
                  )}
                  {t("confluence.create.testConnection")}
                </Button>

                {testStatus !== "idle" && testStatus !== "testing" && (
                  <div
                    className={`mt-3 p-3 rounded-lg flex items-center gap-3 ${
                      testStatus === "success"
                        ? "bg-emerald-50 dark:bg-emerald-950/30 border border-emerald-200 dark:border-emerald-900"
                        : "bg-rose-50 dark:bg-rose-950/30 border border-rose-200 dark:border-rose-900"
                    }`}
                  >
                    {testStatus === "success" ? (
                      <CheckCircle className="h-5 w-5 text-emerald-600 flex-shrink-0" />
                    ) : (
                      <AlertCircle className="h-5 w-5 text-rose-600 flex-shrink-0" />
                    )}
                    <span
                      className={`text-sm ${
                        testStatus === "success" ? "text-emerald-700 dark:text-emerald-400" : "text-rose-700 dark:text-rose-400"
                      }`}
                    >
                      {testMessage}
                    </span>
                  </div>
                )}
              </div>
            </div>
          </Card>

          {/* Sync Settings Card */}
          <Card className="p-6 border-border/60">
            <div className="flex items-center gap-3 mb-6">
              <div className="w-8 h-8 rounded-lg bg-violet-500/10 flex items-center justify-center">
                <RefreshCcw className="h-4 w-4 text-violet-500" />
              </div>
              <h2 className="font-semibold text-foreground">{t("confluence.create.syncSettings")}</h2>
            </div>

            <div className="space-y-5">
              <FormField label={t("confluence.create.syncSettings")} hint={t("confluence.create.pollingIntervalHint")}>
                <div className="grid grid-cols-2 gap-3">
                  <button
                    type="button"
                    onClick={() => updateField("sync_mode", "manual")}
                    className={`p-4 rounded-lg border-2 text-left transition-all ${
                      formData.sync_mode === "manual"
                        ? "border-primary bg-primary/5"
                        : "border-border/60 hover:border-border"
                    }`}
                  >
                    <div className="flex items-center gap-2 mb-2">
                      <User className="h-4 w-4 text-muted-foreground" />
                      <span className="font-medium text-sm">{t("confluence.create.syncModeManual")}</span>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {t("confluence.create.syncModeManualDesc")}
                    </p>
                  </button>

                  <button
                    type="button"
                    onClick={() => updateField("sync_mode", "polling")}
                    className={`p-4 rounded-lg border-2 text-left transition-all ${
                      formData.sync_mode === "polling"
                        ? "border-primary bg-primary/5"
                        : "border-border/60 hover:border-border"
                    }`}
                  >
                    <div className="flex items-center gap-2 mb-2">
                      <Clock className="h-4 w-4 text-muted-foreground" />
                      <span className="font-medium text-sm">{t("confluence.create.syncModeAuto")}</span>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {t("confluence.create.syncModeAutoDesc")}
                    </p>
                  </button>
                </div>
              </FormField>

              {formData.sync_mode === "polling" && (
                <FormField label={t("confluence.create.pollingInterval")} hint={t("confluence.create.pollingIntervalHint")}>
                  <Select
                    value={String(formData.polling_interval_minutes)}
                    onValueChange={(v) => updateField("polling_interval_minutes", Number(v))}
                  >
                    <SelectTrigger className="bg-background">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="15">{t("confluence.create.intervals.15min")}</SelectItem>
                      <SelectItem value="30">{t("confluence.create.intervals.30min")}</SelectItem>
                      <SelectItem value="60">{t("confluence.create.intervals.1hour")}</SelectItem>
                      <SelectItem value="120">{t("confluence.create.intervals.2hours")}</SelectItem>
                      <SelectItem value="360">{t("confluence.create.intervals.6hours")}</SelectItem>
                      <SelectItem value="720">{t("confluence.create.intervals.12hours")}</SelectItem>
                      <SelectItem value="1440">{t("confluence.create.intervals.daily")}</SelectItem>
                    </SelectContent>
                  </Select>
                </FormField>
              )}
            </div>
          </Card>

          {/* Submit */}
          <div className="flex items-center justify-between pt-4">
            <Button
              type="button"
              variant="ghost"
              onClick={() => navigate("/confluence")}
            >
              {t("common.cancel")}
            </Button>

            <div className="flex items-center gap-3">
              {testStatus === "success" && (
                <Badge variant="outline" className="bg-emerald-50 text-emerald-600 border-emerald-200">
                  <CheckCircle className="h-3 w-3 mr-1" />
                  {t("confluence.create.verified")}
                </Badge>
              )}
              <Button
                type="submit"
                disabled={createMutation.isPending}
                className="bg-gradient-to-r from-blue-500 to-cyan-500 hover:from-blue-600 hover:to-cyan-600 text-white border-0 min-w-[140px]"
              >
                {createMutation.isPending ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    {t("confluence.create.creating")}
                  </>
                ) : (
                  <>
                    {t("confluence.create.createButton")}
                    <ArrowLeft className="h-4 w-4 ml-2 rotate-180" />
                  </>
                )}
              </Button>
            </div>
          </div>

          {/* Error display */}
          {createMutation.isError && (
            <Card className="p-4 border-rose-200 bg-rose-50 dark:bg-rose-950/30">
              <div className="flex items-center gap-3">
                <AlertCircle className="h-5 w-5 text-rose-600 flex-shrink-0" />
                <div>
                  <p className="font-medium text-rose-800 dark:text-rose-400">
                    {t("confluence.create.errors.createFailed")}
                  </p>
                  <p className="text-sm text-rose-600 dark:text-rose-500">
                    {createMutation.error instanceof Error
                      ? createMutation.error.message
                      : t("common.error")}
                  </p>
                </div>
              </div>
            </Card>
          )}
        </form>
      </div>
    </div>
  );
}
