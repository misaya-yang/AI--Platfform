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
import { Plus, Server, Cloud, Cpu, Search, ClipboardList } from "lucide-react";
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
import type { Provider, ProviderCreate, ProviderUpdate } from "@/api/providers";
import type { LLMModel, ModelCreate, ModelUpdate } from "@/api/models";
import { ExamsTabContent } from "@/pages/exams";

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
  const services = servicesQuery.data || [];
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
    queryKey: providersApi.providerQueryKeys.all,
    queryFn: () => providersApi.listProviders(true),
  });
  const providers = providersQuery.data || [];
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
    mutationFn: (data: ProviderCreate) => providersApi.createProvider(data),
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
  const handleProviderSubmit = async (data: ProviderCreate | ProviderUpdate) => {
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
    <div className="space-y-4 p-1 pt-3">
      <Tabs value={activeTab} onValueChange={setActiveTab} className="space-y-4">
        <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 border-b border-border/40 pb-3">
          <TabsList className="bg-muted/50 p-1 h-auto rounded-xl">
            <TabsTrigger
              value="services"
              className="gap-2 rounded-lg px-4 py-2 data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm transition-all text-muted-foreground"
            >
              <Server className="h-4 w-4" />
              {t("services.page.tabs.services")}
            </TabsTrigger>
            <TabsTrigger
              value="providers"
              className="gap-2 rounded-lg px-4 py-2 data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm transition-all text-muted-foreground"
            >
              <Cloud className="h-4 w-4" />
              {t("services.page.tabs.providers")}
            </TabsTrigger>
            <TabsTrigger
              value="models"
              className="gap-2 rounded-lg px-4 py-2 data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm transition-all text-muted-foreground"
            >
              <Cpu className="h-4 w-4" />
              {t("services.page.tabs.models")}
            </TabsTrigger>
            <TabsTrigger
              value="exams"
              className="gap-2 rounded-lg px-4 py-2 data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm transition-all text-muted-foreground"
            >
              <ClipboardList className="h-4 w-4" />
              {t("services.page.tabs.exams", "考试管理")}
            </TabsTrigger>
          </TabsList>

          <div className="flex items-center gap-2">
            <div className="relative w-60">
              <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                placeholder={t("services.page.searchPlaceholder", "Search...")}
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                className="pl-9 h-9 bg-background/50 border-border/50 focus:bg-background transition-colors rounded-xl"
              />
            </div>

            {activeTab === "services" && (
              <ServiceForm
                onRegistered={() => qc.invalidateQueries({ queryKey: ["services"] })}
              />
            )}
            {activeTab === "providers" && (
              <Button
                onClick={() => {
                  setEditingProvider(null);
                  setProviderFormOpen(true);
                }}
                className="rounded-full shadow-lg shadow-primary/20 hover:shadow-primary/40 transition-all hover:scale-105 active:scale-95"
              >
                <Plus className="h-4 w-4 mr-2" />
                {t("services.page.addProvider")}
              </Button>
            )}
            {activeTab === "models" && (
              <Button
                onClick={() => {
                  setEditingModel(null);
                  setModelFormOpen(true);
                }}
                disabled={providers.length === 0}
                className="rounded-full shadow-lg shadow-primary/20 hover:shadow-primary/40 transition-all hover:scale-105 active:scale-95"
              >
                <Plus className="h-4 w-4 mr-2" />
                {t("services.page.addModel")}
              </Button>
            )}
          </div>
        </div>

        <div className="min-h-[500px]">
          {/* Services Tab */}
          <TabsContent value="services" className="mt-0 space-y-4 focus-visible:outline-none focus-visible:ring-0">
            {servicesQuery.isLoading ? (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {[1, 2, 3, 4, 5, 6].map((i) => (
                  <div key={i} className="h-24 rounded-xl bg-muted/20 animate-pulse" />
                ))}
              </div>
            ) : filteredServices.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-20 border-2 border-dashed rounded-3xl bg-muted/5">
                <div className="h-20 w-20 rounded-full bg-muted/20 flex items-center justify-center mb-4">
                  <Server className="h-10 w-10 text-muted-foreground/50" />
                </div>
                <p className="text-muted-foreground text-lg font-medium">{t("services.page.noServices")}</p>
                {searchQuery ? (
                  <p className="text-sm text-muted-foreground/60 mt-1">{t("services.search.noResults", "No services match your search.")}</p>
                ) : (
                  <p className="text-sm text-muted-foreground/60 mt-1">{t("services.empty.hint", "Get started by registering your first service.")}</p>
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
                <div className="bg-foreground text-background px-4 py-2 rounded-full shadow-2xl text-sm font-medium animate-in slide-in-from-bottom-5 fade-in">
                  {t("services.selected", "Selected")}: {selectedServiceId}
                </div>
              </div>
            )}
          </TabsContent>

          {/* Providers Tab */}
          <TabsContent value="providers" className="mt-0 space-y-4 focus-visible:outline-none focus-visible:ring-0">
            {providersQuery.isLoading ? (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {[1, 2, 3, 4, 5, 6].map((i) => (
                  <div key={i} className="h-20 rounded-xl bg-muted/20 animate-pulse" />
                ))}
              </div>
            ) : filteredProviders.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-20 border-2 border-dashed rounded-3xl bg-muted/5">
                <div className="h-20 w-20 rounded-full bg-muted/20 flex items-center justify-center mb-4">
                  <Cloud className="h-10 w-10 text-muted-foreground/50" />
                </div>
                <p className="text-muted-foreground text-lg font-medium">{t("services.page.noProviders")}</p>
                {searchQuery && <p className="text-sm text-muted-foreground/60 mt-1">{t("providers.search.noResults", "No providers match your search.")}</p>}
              </div>
            ) : (
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                {filteredProviders.map((p) => (
                  <ProviderCard
                    key={p.provider_id}
                    provider={p}
                    onEdit={() => handleEditProvider(p)}
                    onDelete={() => handleDeleteProvider(p)}
                  />
                ))}
              </div>
            )}
          </TabsContent>

          {/* Models Tab */}
          <TabsContent value="models" className="mt-0 space-y-4 focus-visible:outline-none focus-visible:ring-0">
            {modelsQuery.isLoading ? (
              <div className="space-y-4">
                {[1, 2, 3, 4, 5].map((i) => (
                  <div key={i} className="h-16 rounded-xl bg-muted/20 animate-pulse" />
                ))}
              </div>
            ) : models.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-20 border-2 border-dashed rounded-3xl bg-muted/5">
                <div className="h-20 w-20 rounded-full bg-muted/20 flex items-center justify-center mb-4">
                  <Cpu className="h-10 w-10 text-muted-foreground/50" />
                </div>
                <p className="text-muted-foreground text-lg font-medium">{t("services.page.noModels")}</p>
                {searchQuery && <p className="text-sm text-muted-foreground/60 mt-1">{t("models.search.noResults", "No models match your search.")}</p>}
              </div>
            ) : (
              <div className="bg-background/50 backdrop-blur-sm rounded-2xl border border-border/50 overflow-hidden shadow-sm">
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

          {/* Exams Tab */}
          <TabsContent value="exams" className="mt-0 focus-visible:outline-none focus-visible:ring-0">
            <ExamsTabContent />
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
