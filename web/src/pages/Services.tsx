/**
 * Services Page with Tabs
 *
 * Tabs: Services, Providers, Models
 */

import { useState, useMemo, useEffect, useRef } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Plus, Server, Cloud, Cpu, Search } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useTranslation } from "react-i18next";
import { getErrorMessage } from "@/lib/utils";

import { useServices, useHealth } from "@/hooks/useServices";
import { ServiceCard } from "@/components/ServiceCard";
import { ServiceForm } from "@/components/ServiceForm";
import { useAppStore } from "@/store/useAppStore";

// Provider & Model imports
import { ProviderCard, ProviderForm, ModelTable, ModelForm } from "@/components/llm";
import * as providersApi from "@/api/providers";
import * as modelsApi from "@/api/models";
import type {
  Provider,
  ProviderCreate,
  ProviderFromTemplateCreate,
  ProviderUpdate,
} from "@/api/providers";
import type { LLMModel, ModelCreate, ModelUpdate } from "@/api/models";

export function ServicesPage() {
  const { toast } = useToast();
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useState("services");
  const [searchInput, setSearchInput] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Debounced search to avoid excessive re-renders
  useEffect(() => {
    if (debounceRef.current) {
      clearTimeout(debounceRef.current);
    }
    debounceRef.current = setTimeout(() => {
      setSearchQuery(searchInput);
    }, 300);
    return () => {
      if (debounceRef.current) {
        clearTimeout(debounceRef.current);
      }
    };
  }, [searchInput]);

  // Services state
  const servicesQuery = useServices();
  const healthQuery = useHealth();
  const services = useMemo(
    () => servicesQuery.data ?? [],
    [servicesQuery.data]
  );
  const health = healthQuery.data || {};
  // Filtered Services
  const filteredServices = useMemo(() => {
    let result = services;
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      result = result.filter(s =>
        (s.service_id || "").toLowerCase().includes(q) ||
        (s.description || "").toLowerCase().includes(q)
      );
    }
    return result;
  }, [services, searchQuery]);

  const { selectedServiceId, setSelectedServiceId } = useAppStore();

  // Provider state
  const [providerFormOpen, setProviderFormOpen] = useState(false);
  const [editingProvider, setEditingProvider] = useState<Provider | null>(null);
  const [deleteProviderOpen, setDeleteProviderOpen] = useState(false);
  const [providerToDelete, setProviderToDelete] = useState<Provider | null>(null);

  // Model state
  const [modelFormOpen, setModelFormOpen] = useState(false);
  const [editingModel, setEditingModel] = useState<LLMModel | null>(null);
  const [deleteModelOpen, setDeleteModelOpen] = useState(false);
  const [modelToDelete, setModelToDelete] = useState<LLMModel | null>(null);

  // Provider queries
  const providersQuery = useQuery({
    queryKey: providersApi.providerQueryKeys.list(true),
    queryFn: () => providersApi.listProviders(true),
  });
  const providerTemplatesQuery = useQuery({
    queryKey: providersApi.providerQueryKeys.templates,
    queryFn: providersApi.listProviderTemplates,
  });
  const providers = useMemo(
    () => providersQuery.data ?? [],
    [providersQuery.data]
  );
  const providerMap = providers.reduce(
    (acc, p) => ({ ...acc, [p.provider_id]: p.display_name }),
    {} as Record<string, string>
  );

  // Filtered Providers
  const filteredProviders = useMemo(() => {
    let result = providers;
    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      result = result.filter(p =>
        (p.display_name || "").toLowerCase().includes(q) ||
        (p.provider_id || "").toLowerCase().includes(q) ||
        (p.base_url || "").toLowerCase().includes(q)
      );
    }
    return result;
  }, [providers, searchQuery]);

  // Model queries
  const modelsQuery = useQuery({
    queryKey: modelsApi.modelQueryKeys.all,
    queryFn: () => modelsApi.listModels(undefined, true),
  });

  const models = useMemo(() => {
    if (!modelsQuery.data) return [];
    let result = [...modelsQuery.data].sort((a, b) => {
      // Priority 1: Enabled first
      if (a.is_enabled !== b.is_enabled) {
        return a.is_enabled ? -1 : 1;
      }
      // Priority 2: Sort order (descending)
      if ((a.sort_order || 0) !== (b.sort_order || 0)) {
        return (b.sort_order || 0) - (a.sort_order || 0);
      }
      // Priority 3: Alphabetical by display name
      return a.display_name.localeCompare(b.display_name);
    });

    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      result = result.filter(m =>
        (m.display_name || "").toLowerCase().includes(q) ||
        (m.model_id || "").toLowerCase().includes(q) ||
        (m.provider_id || "").toLowerCase().includes(q)
      );
    }
    return result;
  }, [modelsQuery.data, searchQuery]);

  // Provider mutations
  const createProviderMutation = useMutation({
    mutationFn: (data: ProviderCreate | ProviderFromTemplateCreate) =>
      "template_id" in data
        ? providersApi.createProviderFromTemplate(data)
        : providersApi.createProvider(data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["providers"] });
      setProviderFormOpen(false);
      toast({ title: t("services.page.toast.providerCreated") });
    },
    onError: (err: Error) => {
      toast({ title: t("services.page.toast.createFailed"), description: getErrorMessage(err), variant: "destructive" });
    },
  });

  const updateProviderMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: ProviderUpdate }) =>
      providersApi.updateProvider(id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["providers"] });
      setProviderFormOpen(false);
      setEditingProvider(null);
      toast({ title: t("services.page.toast.providerUpdated") });
    },
    onError: (err: Error) => {
      toast({ title: t("services.page.toast.updateFailed"), description: getErrorMessage(err), variant: "destructive" });
    },
  });

  const deleteProviderMutation = useMutation({
    mutationFn: (id: string) => providersApi.deleteProvider(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["providers"] });
      qc.invalidateQueries({ queryKey: ["models"] });
      setDeleteProviderOpen(false);
      setProviderToDelete(null);
      toast({ title: t("services.page.toast.providerDeleted") });
    },
    onError: (err: Error) => {
      toast({ title: t("services.page.toast.deleteFailed"), description: getErrorMessage(err), variant: "destructive" });
    },
  });

  const syncProviderModelsMutation = useMutation({
    mutationFn: (providerId: string) => providersApi.syncProviderModels(providerId),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["models"] });
      qc.invalidateQueries({ queryKey: ["providers"] });
      toast({
        title: t("services.page.toast.modelsSynced", "Models synced"),
        description: t(
          "services.page.toast.modelsSyncedDescription",
          "Created {{created}}, updated {{updated}}, skipped {{skipped}}.",
          {
            created: result.created_models.length,
            updated: result.updated_models.length,
            skipped: result.skipped_models.length,
          }
        ),
      });
      if (result.discovery_warnings.length > 0) {
        toast({
          title: t("services.page.toast.syncWarnings", "Sync warnings"),
          description: result.discovery_warnings.join(" "),
        });
      }
    },
    onError: (err: Error) => {
      toast({
        title: t("services.page.toast.syncFailed", "Model sync failed"),
        description: getErrorMessage(err),
        variant: "destructive",
      });
    },
  });

  // Model mutations
  const createModelMutation = useMutation({
    mutationFn: (data: ModelCreate) => modelsApi.createModel(data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
      setModelFormOpen(false);
      toast({ title: t("services.page.toast.modelCreated") });
    },
    onError: (err: Error) => {
      toast({ title: t("services.page.toast.createFailed"), description: getErrorMessage(err), variant: "destructive" });
    },
  });

  const updateModelMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: ModelUpdate }) =>
      modelsApi.updateModel(id, data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
      setModelFormOpen(false);
      setEditingModel(null);
      toast({ title: t("services.page.toast.modelUpdated") });
    },
    onError: (err: Error) => {
      toast({ title: t("services.page.toast.updateFailed"), description: getErrorMessage(err), variant: "destructive" });
    },
  });

  const deleteModelMutation = useMutation({
    mutationFn: (id: string) => modelsApi.deleteModel(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
      setDeleteModelOpen(false);
      setModelToDelete(null);
      toast({ title: t("services.page.toast.modelDeleted") });
    },
    onError: (err: Error) => {
      toast({ title: t("services.page.toast.deleteFailed"), description: getErrorMessage(err), variant: "destructive" });
    },
  });

  const toggleModelMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      modelsApi.toggleModel(id, enabled),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (err: Error) => {
      toast({ title: t("services.page.toast.operationFailed"), description: getErrorMessage(err), variant: "destructive" });
    },
  });

  // Reset selected service if not in list
  if (selectedServiceId && services.length > 0 && !services.some(s => s.service_id === selectedServiceId)) {
    setTimeout(() => setSelectedServiceId(undefined), 0);
  }

  // Provider handlers
  const handleProviderSubmit = async (
    data: ProviderCreate | ProviderFromTemplateCreate | ProviderUpdate
  ) => {
    if (editingProvider) {
      await updateProviderMutation.mutateAsync({
        id: editingProvider.provider_id,
        data: data as ProviderUpdate,
      });
    } else {
      await createProviderMutation.mutateAsync(data as ProviderCreate);
    }
  };

  const handleEditProvider = (provider: Provider) => {
    setEditingProvider(provider);
    setProviderFormOpen(true);
  };

  const handleDeleteProvider = (provider: Provider) => {
    setProviderToDelete(provider);
    setDeleteProviderOpen(true);
  };

  // Model handlers
  const handleModelSubmit = async (data: ModelCreate | ModelUpdate) => {
    if (editingModel) {
      await updateModelMutation.mutateAsync({
        id: editingModel.model_id,
        data: data as ModelUpdate,
      });
    } else {
      await createModelMutation.mutateAsync(data as ModelCreate);
    }
  };

  const handleEditModel = (model: LLMModel) => {
    setEditingModel(model);
    setModelFormOpen(true);
  };

  const handleDeleteModel = (model: LLMModel) => {
    setModelToDelete(model);
    setDeleteModelOpen(true);
  };

  const handleToggleModel = async (model: LLMModel, enabled: boolean) => {
    await toggleModelMutation.mutateAsync({ id: model.model_id, enabled });
  };

  return (
    <div className="space-y-4 p-1 pt-2">
      {/* 基础设施运行态概览条 */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 p-3 rounded-xl border border-border/70 bg-card/60">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-emerald-500/10 text-emerald-500 shrink-0">
            <span className="relative flex h-2.5 w-2.5">
              <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500"></span>
            </span>
          </div>
          <div className="min-w-0">
            <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">{t("services.overview.gatewayState")}</div>
            <div className="text-sm font-bold tabular-nums text-foreground flex items-center gap-1.5">
              {services.length > 0 ? t("services.overview.onlineCount", { count: services.length }) : t("services.overview.offlineCount")}
              <span className="text-[10px] font-semibold text-emerald-600 dark:text-emerald-400 bg-emerald-500/10 px-1 py-0.2 rounded-xs">{t("services.overview.available")}</span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary shrink-0">
            <Cloud className="h-4.5 w-4.5" />
          </div>
          <div className="min-w-0">
            <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">{t("services.overview.providers")}</div>
            <div className="text-sm font-bold tabular-nums text-foreground">
              {providers.length} <span className="text-xs font-normal text-muted-foreground">{t("services.overview.connected")}</span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-indigo-500/10 text-indigo-500 shrink-0">
            <Cpu className="h-4.5 w-4.5" />
          </div>
          <div className="min-w-0">
            <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">{t("services.overview.models")}</div>
            <div className="text-sm font-bold tabular-nums text-foreground">
              {(modelsQuery.data || []).length} <span className="text-xs font-normal text-muted-foreground">{t("services.overview.activeModels")}</span>
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-amber-500/10 text-amber-500 shrink-0">
            <Server className="h-4.5 w-4.5" />
          </div>
          <div className="min-w-0">
            <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">{t("services.overview.dispatch")}</div>
            <div className="text-sm font-bold text-foreground truncate">
              {t("services.overview.fallback")}
            </div>
          </div>
        </div>
      </div>

      <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-4">
        <div className="flex flex-col items-stretch justify-between gap-3 border-b border-border/40 pb-3 sm:flex-row sm:items-center">
          <div className="ui-scroll-affordance w-full pb-1 sm:w-auto sm:pb-0">
            <TabsList className="h-auto min-w-max justify-start rounded-lg bg-muted/50 p-1">
              <TabsTrigger
                value="services"
                className="gap-2 rounded-md px-3 py-2 text-muted-foreground transition-colors data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-xs sm:px-4"
              >
                <Server className="h-4 w-4" />
                {t("services.page.tabs.services")}
              </TabsTrigger>
              <TabsTrigger
                value="providers"
                className="gap-2 rounded-md px-3 py-2 text-muted-foreground transition-colors data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-xs sm:px-4"
              >
                <Cloud className="h-4 w-4" />
                {t("services.page.tabs.providers")}
              </TabsTrigger>
              <TabsTrigger
                value="models"
                className="gap-2 rounded-md px-3 py-2 text-muted-foreground transition-colors data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-xs sm:px-4"
              >
                <Cpu className="h-4 w-4" />
                {t("services.page.tabs.models")}
              </TabsTrigger>
            </TabsList>
          </div>

          <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center">
            <div className="relative w-full sm:w-60">
              <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                aria-label={t("services.page.searchPlaceholder", "Search...")}
                placeholder={t("services.page.searchPlaceholder", "Search...")}
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                className="h-9 rounded-md border-border/60 bg-background pl-9 transition-colors focus:bg-background"
              />
            </div>

            {activeTab === "services" && (
              <div className="w-full sm:w-auto [&_button]:w-full sm:[&_button]:w-auto">
                <ServiceForm
                  onRegistered={() => qc.invalidateQueries({ queryKey: ["services"] })}
                />
              </div>
            )}
            {activeTab === "providers" && (
              <Button
                variant="primary"
                onClick={() => {
                  setEditingProvider(null);
                  setProviderFormOpen(true);
                }}
                className="h-9 w-full px-3 sm:w-auto"
              >
                <Plus className="h-4 w-4 mr-2" />
                {t("services.page.addProvider")}
              </Button>
            )}
            {activeTab === "models" && (
              <Button
                variant="primary"
                onClick={() => {
                  setEditingModel(null);
                  setModelFormOpen(true);
                }}
                disabled={providers.length === 0}
                className="h-9 w-full px-3 sm:w-auto"
              >
                <Plus className="h-4 w-4 mr-2" />
                {t("services.page.addModel")}
              </Button>
            )}
          </div>
        </div>

        <div className="min-h-[500px]">
          {/* Services Tab */}
          <TabsContent value="services" className="mt-0 space-y-4 focus-visible:outline-hidden focus-visible:ring-0">
            {servicesQuery.isLoading ? (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {[1, 2, 3, 4, 5, 6].map((i) => (
                  <div key={i} className="h-24 rounded-lg bg-muted/20 animate-pulse" />
                ))}
              </div>
            ) : filteredServices.length === 0 ? (
              <div className="flex flex-col items-center justify-center rounded-xl border border-dashed bg-card/40 px-6 py-14 text-center">
                <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-lg bg-muted/50">
                  <Server className="h-6 w-6 text-muted-foreground/60" />
                </div>
                <p className="font-medium text-foreground">{t("services.page.noServices")}</p>
                {searchQuery ? (
                  <p className="mt-1 text-sm text-muted-foreground">{t("services.search.noResults", "No services match your search.")}</p>
                ) : (
                  <p className="mt-1 text-sm text-muted-foreground">{t("services.empty.hint", "Get started by registering your first service.")}</p>
                )}
              </div>
            ) : (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {filteredServices.map((s) => (
                  <ServiceCard
                    key={s.service_id}
                    service={s}
                    health={health[s.service_id]}
                    selected={selectedServiceId === s.service_id}
                    onSelect={() => setSelectedServiceId(s.service_id)}
                  />
                ))}
              </div>
            )}
            {selectedServiceId && services.some(s => s.service_id === selectedServiceId) && (
              <div className="fixed bottom-6 right-6 z-50">
                <div className="animate-in rounded-lg border border-border bg-card px-4 py-2 text-sm font-medium text-foreground shadow-sm slide-in-from-bottom-5 fade-in">
                  {t("services.selected", "Selected")}: {selectedServiceId}
                </div>
              </div>
            )}
          </TabsContent>

          {/* Providers Tab */}
          <TabsContent value="providers" className="mt-0 space-y-4 focus-visible:outline-hidden focus-visible:ring-0">
            {providersQuery.isLoading ? (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {[1, 2, 3, 4, 5, 6].map((i) => (
                  <div key={i} className="h-20 rounded-lg bg-muted/20 animate-pulse" />
                ))}
              </div>
            ) : filteredProviders.length === 0 ? (
              <div className="flex flex-col items-center justify-center rounded-xl border border-dashed bg-card/40 px-6 py-14 text-center">
                <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-lg bg-muted/50">
                  <Cloud className="h-6 w-6 text-muted-foreground/60" />
                </div>
                <p className="font-medium text-foreground">{t("services.page.noProviders")}</p>
                {searchQuery && <p className="mt-1 text-sm text-muted-foreground">{t("providers.search.noResults", "No providers match your search.")}</p>}
              </div>
            ) : (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {filteredProviders.map((p) => (
                  <ProviderCard
                    key={p.provider_id}
                    provider={p}
                    onEdit={() => handleEditProvider(p)}
                    onDelete={() => handleDeleteProvider(p)}
                    onSyncModels={() => syncProviderModelsMutation.mutate(p.provider_id)}
                    syncingModels={
                      syncProviderModelsMutation.isPending &&
                      syncProviderModelsMutation.variables === p.provider_id
                    }
                  />
                ))}
              </div>
            )}
          </TabsContent>

          {/* Models Tab */}
          <TabsContent value="models" className="mt-0 space-y-4 focus-visible:outline-hidden focus-visible:ring-0">
            {modelsQuery.isLoading ? (
              <div className="space-y-4">
                {[1, 2, 3, 4, 5].map((i) => (
                  <div key={i} className="h-16 rounded-lg bg-muted/20 animate-pulse" />
                ))}
              </div>
            ) : models.length === 0 ? (
              <div className="flex flex-col items-center justify-center rounded-xl border border-dashed bg-card/40 px-6 py-14 text-center">
                <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-lg bg-muted/50">
                  <Cpu className="h-6 w-6 text-muted-foreground/60" />
                </div>
                <p className="font-medium text-foreground">{t("services.page.noModels")}</p>
                {searchQuery && <p className="mt-1 text-sm text-muted-foreground">{t("models.search.noResults", "No models match your search.")}</p>}
              </div>
            ) : (
              <div className="overflow-hidden rounded-lg border border-border/60 bg-card">
                <ModelTable
                  models={models}
                  providers={providerMap}
                  onEdit={handleEditModel}
                  onDelete={handleDeleteModel}
                  onToggle={handleToggleModel}
                  loading={toggleModelMutation.isPending}
                />
              </div>
            )}
          </TabsContent>
        </div>
      </Tabs>

      {/* Dialogs and Modals remain unchanged in logic but inserted here */}
      <ProviderForm
        open={providerFormOpen}
        onOpenChange={(open) => {
          setProviderFormOpen(open);
          if (!open) setEditingProvider(null);
        }}
        provider={editingProvider}
        templates={providerTemplatesQuery.data || []}
        templatesLoading={providerTemplatesQuery.isLoading}
        onSubmit={handleProviderSubmit}
        loading={createProviderMutation.isPending || updateProviderMutation.isPending}
      />

      <ModelForm
        open={modelFormOpen}
        onOpenChange={(open) => {
          setModelFormOpen(open);
          if (!open) setEditingModel(null);
        }}
        model={editingModel}
        providers={providers}
        providerTemplates={providerTemplatesQuery.data || []}
        providersLoading={providersQuery.isLoading}
        providerTemplatesLoading={providerTemplatesQuery.isLoading}
        onSubmit={handleModelSubmit}
        loading={createModelMutation.isPending || updateModelMutation.isPending}
      />

      <AlertDialog open={deleteProviderOpen} onOpenChange={setDeleteProviderOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("services.page.deleteProvider.title")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("services.page.deleteProvider.description", { name: providerToDelete?.display_name })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("services.page.deleteProvider.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => providerToDelete && deleteProviderMutation.mutate(providerToDelete.provider_id)}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteProviderMutation.isPending ? t("services.page.deleteProvider.deleting") : t("services.page.deleteProvider.confirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={deleteModelOpen} onOpenChange={setDeleteModelOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("services.page.deleteModel.title")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("services.page.deleteModel.description", { name: modelToDelete?.display_name })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("services.page.deleteModel.cancel")}</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => modelToDelete && deleteModelMutation.mutate(modelToDelete.model_id)}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteModelMutation.isPending ? t("services.page.deleteModel.deleting") : t("services.page.deleteModel.confirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
