/**
 * ConnectorsPanel — quick access to third-party data connectors from Chat.
 *
 * Backed by the connector stack (src/api/v1/connectors.py):
 * - GET  /api/v1/connectors/available          — catalog + connection status
 * - GET  /api/v1/connectors/auth/{provider}    — start OAuth (returns auth_url)
 * - POST /api/v1/connectors/{provider}/activate — expose MCP tools
 * - GET  /api/v1/connectors/{provider}/mcp-status
 * - DELETE /api/v1/connectors/{provider}       — disconnect
 *
 * Canonical management surface lives at /settings/connectors.
 */

import { useState, useEffect, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { Plug } from "lucide-react";
import { api } from "@/lib/api";

interface ConnectorsPanelProps {
  open: boolean;
  onClose: () => void;
  onCountChange?: (count: number) => void;
}

interface ConnectorInfo {
  provider: string;
  display_name: string;
  description?: string | null;
  icon_url?: string | null;
  enabled: boolean;
  connected: boolean;
  status?: string | null;
}

interface ConnectorMcpStatus {
  mcp_active?: boolean;
  tools?: Array<{ name: string; description: string }>;
}

interface ConnectorActivateResponse extends ConnectorMcpStatus {
  tool_count?: number;
}

function apiErrorMessage(error: unknown, fallback: string): string {
  if (error && typeof error === "object") {
    const maybe = error as {
      response?: { data?: { detail?: unknown } };
      message?: unknown;
    };
    const detail = maybe.response?.data?.detail;
    if (typeof detail === "string" && detail) return detail;
    if (typeof maybe.message === "string" && maybe.message) return maybe.message;
  }
  return fallback;
}

export default function ConnectorsPanel({ open, onClose, onCountChange }: ConnectorsPanelProps) {
  const { t } = useTranslation();
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [mcpByProvider, setMcpByProvider] = useState<Record<string, ConnectorMcpStatus>>({});
  const [busyProvider, setBusyProvider] = useState<string | null>(null);
  const [error, setError] = useState("");

  const loadConnectors = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get<ConnectorInfo[]>("/api/v1/connectors/available");
      setConnectors(data);
      onCountChange?.(data.filter((c) => c.connected).length);
      for (const c of data.filter((x) => x.connected)) {
        try {
          const { data: status } = await api.get<ConnectorMcpStatus>(
            `/api/v1/connectors/${c.provider}/mcp-status`,
          );
          setMcpByProvider((prev) => ({ ...prev, [c.provider]: status }));
        } catch {
          /* MCP status is best-effort */
        }
      }
    } catch {
      onCountChange?.(0);
    }
    setLoading(false);
  }, [onCountChange]);

  useEffect(() => {
    if (!open) return;
    void loadConnectors();
  }, [open, loadConnectors]);

  const handleConnect = async (provider: string) => {
    setBusyProvider(provider);
    setError("");
    try {
      const { data } = await api.get<{ auth_url: string }>(`/api/v1/connectors/auth/${provider}`);
      window.location.href = data.auth_url;
    } catch (err: unknown) {
      setError(apiErrorMessage(err, t("connectors.authFailed")));
      setBusyProvider(null);
    }
  };

  const handleActivate = async (provider: string) => {
    setBusyProvider(provider);
    try {
      const { data } = await api.post<ConnectorActivateResponse>(
        `/api/v1/connectors/${provider}/activate`,
      );
      setMcpByProvider((prev) => ({ ...prev, [provider]: data }));
      alert(t("connectors.activatedToast", { count: data.tool_count ?? data.tools?.length ?? 0 }));
    } catch (err: unknown) {
      alert(t("connectors.activateFailed", { error: apiErrorMessage(err, t("connectors.unknownError")) }));
    }
    setBusyProvider(null);
  };

  const handleDisconnect = async (provider: string) => {
    if (!confirm(t("connectors.disconnectConfirm"))) return;
    setBusyProvider(provider);
    try {
      await api.delete(`/api/v1/connectors/${provider}`);
      setMcpByProvider((prev) => {
        const next = { ...prev };
        delete next[provider];
        return next;
      });
      void loadConnectors();
    } catch (err: unknown) {
      setError(apiErrorMessage(err, t("connectors.disconnectFailed")));
    }
    setBusyProvider(null);
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div className="bg-white dark:bg-zinc-900 rounded-xl shadow-2xl w-[560px] max-h-[80vh] overflow-hidden"
           onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-700">
          <div>
            <h2 className="text-lg font-semibold">{t("connectors.title")}</h2>
            <p className="text-sm text-zinc-500">{t("connectors.subtitle")}</p>
          </div>
          <button type="button" onClick={onClose} className="p-2 rounded-lg hover:bg-zinc-100 dark:hover:bg-zinc-800 transition" aria-label={t("connectors.closeAria")}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M4 4L12 12M12 4L4 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
            </svg>
          </button>
        </div>

        {/* Content */}
        <div className="px-6 py-4 space-y-4 overflow-y-auto max-h-[60vh]">
          {loading ? (
            <div className="text-sm text-zinc-500 py-4 text-center">{t("connectors.loading")}</div>
          ) : connectors.length === 0 ? (
            <div className="text-sm text-zinc-500 py-4 text-center">
              {t("connectors.empty")}
            </div>
          ) : (
            connectors.map((conn) => {
              const mcp = mcpByProvider[conn.provider];
              return (
                <div key={conn.provider} className="rounded-lg border border-zinc-200 dark:border-zinc-700 overflow-hidden">
                  <div className="flex items-center gap-3 p-3">
                    {conn.icon_url ? (
                      <img
                        src={conn.icon_url}
                        alt=""
                        className="h-5 w-5 rounded object-contain"
                      />
                    ) : (
                      <Plug className="h-5 w-5 text-zinc-500" aria-hidden="true" />
                    )}
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium">{conn.display_name}</div>
                      <div className="text-xs text-zinc-500 truncate">{conn.description}</div>
                    </div>
                    <div className={`w-2 h-2 rounded-full ${conn.connected ? "bg-green-500" : "bg-zinc-400"}`} />
                  </div>

                  <div className="px-3 pb-3 flex items-center justify-between">
                    {conn.connected ? (
                      <>
                        <span className="text-xs text-green-600 dark:text-green-400">
                          {mcp?.mcp_active
                            ? t("connectors.activated", { count: mcp.tools?.length ?? 0 })
                            : t("connectors.connectedNoTools")}
                        </span>
                        <div className="flex gap-2">
                          {!mcp?.mcp_active && (
                            <button
                              onClick={() => handleActivate(conn.provider)}
                              disabled={busyProvider === conn.provider}
                              aria-label={`${t("connectors.activate")} ${conn.display_name}`}
                              className="px-3 py-1 text-xs font-medium text-white bg-violet-600 hover:bg-violet-700 rounded-lg transition disabled:opacity-50"
                            >
                              {busyProvider === conn.provider ? t("connectors.processing") : t("connectors.activate")}
                            </button>
                          )}
                          <button
                            onClick={() => handleDisconnect(conn.provider)}
                            disabled={busyProvider === conn.provider}
                            aria-label={`${t("connectors.disconnect")} ${conn.display_name}`}
                            className="px-2 py-1 text-xs text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded transition"
                          >
                            {t("connectors.disconnect")}
                          </button>
                        </div>
                      </>
                    ) : (
                      <>
                        <span className="text-xs text-zinc-400">{t("connectors.notConnected")}</span>
                        <button
                          onClick={() => handleConnect(conn.provider)}
                          disabled={busyProvider === conn.provider}
                          aria-label={`${t("connectors.connect")} ${conn.display_name}`}
                          className="px-3 py-1 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition disabled:opacity-50"
                        >
                          {busyProvider === conn.provider ? t("connectors.redirecting") : t("connectors.connect")}
                        </button>
                      </>
                    )}
                  </div>
                </div>
              );
            })
          )}

          {error && <div className="text-xs text-red-500">{error}</div>}
        </div>

        {/* Footer */}
        <div className="px-6 py-3 border-t border-zinc-200 dark:border-zinc-700 flex items-center justify-between">
          <span className="text-xs text-zinc-400">{t("connectors.footer")}</span>
        </div>
      </div>
    </div>
  );
}
