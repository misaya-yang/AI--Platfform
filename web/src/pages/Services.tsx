/**
 * Services Page with Tabs
 *
 * Tabs: Services, Providers, Models
 */

import { useState } from "react";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
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
import { Plus, Server, Cloud, Cpu } from "lucide-react";
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

export function ServicesPage() {
  const { toast } = useToast();
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [activeTab, setActiveTab] = useState("services");

  // Services state
  const servicesQuery = useServices();
  const healthQuery = useHealth();
  const services = servicesQuery.data || [];
  const health = healthQuery.data || {};
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
    queryKey: ["providers"],
    queryFn: () => providersApi.listProviders(true),
  });
  const providers = providersQuery.data || [];
  const providerMap = providers.reduce(
    (acc, p) => ({ ...acc, [p.provider_id]: p.display_name }),
    {} as Record<string, string>
  );

  // Model queries
  const modelsQuery = useQuery({
    queryKey: ["models"],
    queryFn: () => modelsApi.listModels(undefined, true),
  });
  const models = modelsQuery.data || [];

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
    <div className="space-y-4">
      <Tabs value={activeTab} onValueChange={setActiveTab}>
        <div className="flex items-center justify-between mb-4">
          <TabsList>
            <TabsTrigger value="services" className="gap-2">
              <Server className="h-4 w-4" />
              {t("services.page.tabs.services")}
            </TabsTrigger>
            <TabsTrigger value="providers" className="gap-2">
              <Cloud className="h-4 w-4" />
              {t("services.page.tabs.providers")}
            </TabsTrigger>
            <TabsTrigger value="models" className="gap-2">
              <Cpu className="h-4 w-4" />
              {t("services.page.tabs.models")}
            </TabsTrigger>
          </TabsList>

          {/* Tab-specific actions */}
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
            >
              <Plus className="h-4 w-4 mr-2" />
              {t("services.page.addModel")}
            </Button>
          )}
        </div>

        {/* Services Tab */}
        <TabsContent value="services" className="mt-0">
          {servicesQuery.isLoading ? (
            <div className="text-sm text-muted-foreground">{t("services.page.loading")}</div>
          ) : services.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 border rounded-lg bg-muted/10 border-dashed">
              <p className="text-sm text-muted-foreground">{t("services.page.noServices")}</p>
            </div>
          ) : (
            <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
              {services.map((s) => (
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
            <div className="text-xs text-muted-foreground mt-4">
              {t("services.page.selected")}: {selectedServiceId}
            </div>
          )}
        </TabsContent>

        {/* Providers Tab */}
        <TabsContent value="providers" className="mt-0">
          {providersQuery.isLoading ? (
            <div className="text-sm text-muted-foreground">{t("services.page.loading")}</div>
          ) : providers.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 border rounded-lg bg-muted/10 border-dashed">
              <p className="text-sm text-muted-foreground">{t("services.page.noProviders")}</p>
            </div>
          ) : (
            <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
              {providers.map((p) => (
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
        <TabsContent value="models" className="mt-0">
          {modelsQuery.isLoading ? (
            <div className="text-sm text-muted-foreground">{t("services.page.loading")}</div>
          ) : providers.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 border rounded-lg bg-muted/10 border-dashed">
              <p className="text-sm text-muted-foreground">
                {t("services.page.noModels")}
              </p>
            </div>
          ) : (
            <ModelTable
              models={models}
              providers={providerMap}
              onEdit={handleEditModel}
              onDelete={handleDeleteModel}
              onToggle={handleToggleModel}
              loading={toggleModelMutation.isPending}
            />
          )}
        </TabsContent>
      </Tabs>

      {/* Provider Form Dialog */}
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

      {/* Model Form Dialog */}
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

      {/* Delete Provider Confirmation */}
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

      {/* Delete Model Confirmation */}
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
