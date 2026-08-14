/**
 * Connector catalog settings — manage connector_configs provider definitions.
 *
 * Lists providers, creates/edits definitions (client_secret is write-only),
 * toggles `enabled`, and deletes with the connected-users guard.
 */

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Pencil, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useToast } from "@/hooks/use-toast";
import { getErrorMessage } from "@/lib/utils";
import {
  connectorQueryKeys,
  createConnector,
  deleteConnector,
  listConnectors,
  toggleConnector,
  updateConnector,
  type ConnectorMode,
  type ConnectorProvider,
  type ConnectorProviderCreate,
  type ConnectorProviderUpdate,
  type ConnectorMcpToolInfo,
} from "@/api/connectors";

const MODE_LABELS: Record<ConnectorMode, string> = {
  live: "modeLive",
  ingest: "modeIngest",
  both: "modeBoth",
};

interface FormState {
  provider: string;
  displayName: string;
  description: string;
  mode: ConnectorMode;
  enabled: boolean;
  supportsSync: boolean;
  supportsSearch: boolean;
  clientId: string;
  clientSecret: string;
  authUrl: string;
  tokenUrl: string;
  scopes: string;
  mcpTools: ConnectorMcpToolInfo[];
}

function toForm(row: ConnectorProvider | null): FormState {
  return {
    provider: row?.provider ?? "",
    displayName: row?.display_name ?? "",
    description: row?.description ?? "",
    mode: row?.mode ?? "live",
    enabled: row?.enabled ?? true,
    supportsSync: row?.supports_sync ?? false,
    supportsSearch: row?.supports_search ?? true,
    clientId: row?.auth?.client_id ?? "",
    clientSecret: "",
    authUrl: row?.auth?.auth_url ?? "",
    tokenUrl: row?.auth?.token_url ?? "",
    scopes: row?.auth?.scopes ?? "",
    mcpTools: row?.mcp_tools ?? [],
  };
}

function fromForm(form: FormState): ConnectorProviderCreate {
  const auth = {
    client_id: form.clientId,
    client_secret: form.clientSecret || null,
    auth_url: form.authUrl,
    token_url: form.tokenUrl,
    scopes: form.scopes,
  };
  return {
    provider: form.provider,
    display_name: form.displayName,
    description: form.description || null,
    mode: form.mode,
    enabled: form.enabled,
    supports_sync: form.supportsSync,
    supports_search: form.supportsSearch,
    auth,
    mcp_tools: form.mcpTools,
  };
}

export function ConnectorsSettingsPage() {
  const { t } = useTranslation();
  const { toast } = useToast();
  const qc = useQueryClient();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<ConnectorProvider | null>(null);
  const [form, setForm] = useState<FormState>(() => toForm(null));

  const { data: connectors = [], isLoading } = useQuery({
    queryKey: connectorQueryKeys.list(),
    queryFn: listConnectors,
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: connectorQueryKeys.all });

  const saveMutation = useMutation({
    mutationFn: async (formState: FormState) => {
      const payload = fromForm(formState);
      if (editing) {
        const updates: ConnectorProviderUpdate = {
          display_name: payload.display_name,
          description: payload.description,
          mode: payload.mode,
          enabled: payload.enabled,
          supports_sync: payload.supports_sync,
          supports_search: payload.supports_search,
          auth: payload.auth,
          mcp_tools: payload.mcp_tools,
        };
        return updateConnector(editing.provider, updates);
      }
      return createConnector(payload);
    },
    onSuccess: () => {
      invalidate();
      setDialogOpen(false);
      toast({ title: t("settings.connectors.updateSuccess") });
    },
    onError: (error: unknown) => {
      toast({ title: getErrorMessage(error) ?? t("settings.connectors.failed"), variant: "destructive" });
    },
  });

  const toggleMutation = useMutation({
    mutationFn: (row: ConnectorProvider) => toggleConnector(row.provider, !row.enabled),
    onSuccess: () => invalidate(),
    onError: (error: unknown) => {
      toast({ title: getErrorMessage(error) ?? t("settings.connectors.failed"), variant: "destructive" });
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (row: ConnectorProvider) => deleteConnector(row.provider),
    onSuccess: () => {
      invalidate();
      toast({ title: t("settings.connectors.deleteSuccess") });
    },
    onError: (error: unknown) => {
      const message = getErrorMessage(error) ?? "";
      toast({
        title: message.includes("connected users")
          ? t("settings.connectors.deleteGuard")
          : message || t("settings.connectors.failed"),
        variant: "destructive",
      });
    },
  });

  const openCreate = () => {
    setEditing(null);
    setForm(toForm(null));
    setDialogOpen(true);
  };

  const openEdit = (row: ConnectorProvider) => {
    setEditing(row);
    setForm(toForm(row));
    setDialogOpen(true);
  };

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const setTool = (index: number, field: keyof ConnectorMcpToolInfo, value: string) => {
    setForm((prev) => {
      const mcpTools = prev.mcpTools.map((tool, i) =>
        i === index ? { ...tool, [field]: value } : tool
      );
      return { ...prev, mcpTools };
    });
  };

  const addTool = () => set("mcpTools", [...form.mcpTools, { name: "", description: "" }]);
  const removeTool = (index: number) =>
    set(
      "mcpTools",
      form.mcpTools.filter((_, i) => i !== index)
    );

  const sorted = useMemo(
    () => [...connectors].sort((a, b) => a.display_name.localeCompare(b.display_name)),
    [connectors]
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">{t("settings.connectors.title")}</h1>
          <p className="text-muted-foreground">{t("settings.connectors.description")}</p>
        </div>
        <Button onClick={openCreate}>
          <Plus className="mr-2 h-4 w-4" />
          {t("settings.connectors.newConnector")}
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">{t("settings.connectors.listTitle")}</CardTitle>
          <CardDescription>{t("settings.connectors.description")}</CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <p className="text-sm text-muted-foreground">Loading...</p>
          ) : sorted.length === 0 ? (
            <p className="text-sm text-muted-foreground">{t("settings.connectors.empty")}</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("settings.connectors.column.provider")}</TableHead>
                  <TableHead>{t("settings.connectors.column.displayName")}</TableHead>
                  <TableHead>{t("settings.connectors.column.mode")}</TableHead>
                  <TableHead>{t("settings.connectors.column.status")}</TableHead>
                  <TableHead className="text-right">{t("settings.connectors.column.actions")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sorted.map((row) => (
                  <TableRow key={row.provider}>
                    <TableCell className="font-medium">{row.provider}</TableCell>
                    <TableCell>{row.display_name}</TableCell>
                    <TableCell>
                      <Badge variant="outline">{t(`settings.connectors.${MODE_LABELS[row.mode]}`)}</Badge>
                    </TableCell>
                    <TableCell>
                      <Switch
                        checked={row.enabled}
                        onCheckedChange={() => toggleMutation.mutate(row)}
                        aria-label={t("settings.connectors.enabled")}
                      />
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        <Button variant="ghost" size="sm" onClick={() => openEdit(row)}>
                          <Pencil className="h-4 w-4" />
                        </Button>
                        <AlertDialog>
                          <AlertDialogTrigger asChild>
                            <Button variant="ghost" size="sm">
                              <Trash2 className="h-4 w-4" />
                            </Button>
                          </AlertDialogTrigger>
                          <AlertDialogContent>
                            <AlertDialogHeader>
                              <AlertDialogTitle>{t("settings.connectors.deleteConfirm")}</AlertDialogTitle>
                              <AlertDialogDescription>
                                {row.display_name} ({row.provider})
                              </AlertDialogDescription>
                            </AlertDialogHeader>
                            <AlertDialogFooter>
                              <AlertDialogCancel>{t("settings.connectors.cancel")}</AlertDialogCancel>
                              <AlertDialogAction onClick={() => deleteMutation.mutate(row)}>
                                {t("settings.connectors.deleteConnector")}
                              </AlertDialogAction>
                            </AlertDialogFooter>
                          </AlertDialogContent>
                        </AlertDialog>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {editing ? t("settings.connectors.editConnector") : t("settings.connectors.newConnector")}
            </DialogTitle>
            <DialogDescription>{t("settings.connectors.description")}</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <Label>{t("settings.connectors.field.provider")}</Label>
              <Input
                value={form.provider}
                disabled={!!editing}
                placeholder={t("settings.connectors.field.providerPlaceholder")}
                onChange={(e) => set("provider", e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>{t("settings.connectors.field.displayName")}</Label>
              <Input value={form.displayName} onChange={(e) => set("displayName", e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label>{t("settings.connectors.field.description")}</Label>
              <Textarea
                value={form.description}
                onChange={(e) => set("description", e.target.value)}
              />
            </div>
            <div className="space-y-2">
              <Label>{t("settings.connectors.field.mode")}</Label>
              <Select value={form.mode} onValueChange={(v) => set("mode", v as ConnectorMode)}>
                <SelectTrigger className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(MODE_LABELS) as ConnectorMode[]).map((mode) => (
                    <SelectItem key={mode} value={mode}>
                      {t(`settings.connectors.${MODE_LABELS[mode]}`)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">{t("settings.connectors.field.modeHint")}</p>
            </div>
            <div className="flex items-center gap-4">
              <Label className="flex items-center gap-2">
                <Switch checked={form.enabled} onCheckedChange={(v) => set("enabled", v)} />
                {t("settings.connectors.field.enabled")}
              </Label>
              <Label className="flex items-center gap-2">
                <Switch checked={form.supportsSync} onCheckedChange={(v) => set("supportsSync", v)} />
                {t("settings.connectors.field.supportsSync")}
              </Label>
              <Label className="flex items-center gap-2">
                <Switch checked={form.supportsSearch} onCheckedChange={(v) => set("supportsSearch", v)} />
                {t("settings.connectors.field.supportsSearch")}
              </Label>
            </div>

            <div className="space-y-2 border-t pt-4">
              <h4 className="text-sm font-medium">{t("settings.connectors.field.authUrl")}</h4>
              <div className="space-y-2">
                <div className="grid grid-cols-2 gap-2">
                  <div className="space-y-1">
                    <Label>{t("settings.connectors.field.clientId")}</Label>
                    <Input value={form.clientId} onChange={(e) => set("clientId", e.target.value)} />
                  </div>
                  <div className="space-y-1">
                    <Label>{t("settings.connectors.field.clientSecret")}</Label>
                    <Input
                      type="password"
                      value={form.clientSecret}
                      placeholder={editing ? t("settings.connectors.field.clientSecretPlaceholder") : ""}
                      onChange={(e) => set("clientSecret", e.target.value)}
                    />
                  </div>
                </div>
                <div className="space-y-1">
                  <Label>{t("settings.connectors.field.authUrl")}</Label>
                  <Input value={form.authUrl} onChange={(e) => set("authUrl", e.target.value)} />
                </div>
                <div className="space-y-1">
                  <Label>{t("settings.connectors.field.tokenUrl")}</Label>
                  <Input value={form.tokenUrl} onChange={(e) => set("tokenUrl", e.target.value)} />
                </div>
                <div className="space-y-1">
                  <Label>{t("settings.connectors.field.scopes")}</Label>
                  <Input value={form.scopes} onChange={(e) => set("scopes", e.target.value)} />
                </div>
              </div>
            </div>

            <div className="space-y-2 border-t pt-4">
              <div className="flex items-center justify-between">
                <h4 className="text-sm font-medium">{t("settings.connectors.field.mcpTools")}</h4>
                <Button variant="outline" size="sm" onClick={addTool}>
                  <Plus className="mr-1 h-3 w-3" />
                  {t("settings.connectors.newConnector")}
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">{t("settings.connectors.field.mcpToolsHint")}</p>
              {form.mcpTools.map((tool, index) => (
                <div key={index} className="grid grid-cols-[1fr_auto] gap-2">
                  <div className="grid grid-cols-2 gap-2">
                    <Input
                      value={tool.name ?? ""}
                      placeholder={t("settings.connectors.field.toolName")}
                      onChange={(e) => setTool(index, "name", e.target.value)}
                    />
                    <Input
                      value={tool.description ?? ""}
                      placeholder={t("settings.connectors.field.toolDescription")}
                      onChange={(e) => setTool(index, "description", e.target.value)}
                    />
                  </div>
                  <Button variant="ghost" size="sm" onClick={() => removeTool(index)}>
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              ))}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              {t("settings.connectors.cancel")}
            </Button>
            <Button onClick={() => saveMutation.mutate(form)} disabled={saveMutation.isPending}>
              {saveMutation.isPending ? t("settings.connectors.saving") : t("settings.connectors.save")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
