/**
 * ConnectorsPanel — manage third-party integrations (Confluence, Outlook, GitHub, Gmail).
 *
 * Shows available connectors with connect/disconnect and search UI.
 * Opened from the "+" quick actions menu or settings.
 */

import { useState, useEffect, useCallback } from "react";
import {
  listAvailableConnectors,
  initiateOAuth,
  disconnectConnector,
  type ConnectorInfo,
} from "@/api/connectors";
import { useTranslation } from "react-i18next";

// Provider icon mapping
const PROVIDER_ICONS: Record<string, string> = {
  confluence: "📄",
  outlook: "📧",
  github: "🐙",
  gmail: "✉️",
  google_drive: "📁",
  slack: "💬",
};

interface ConnectorsPanelProps {
  open: boolean;
  onClose: () => void;
}

export default function ConnectorsPanel({ open, onClose }: ConnectorsPanelProps) {
  const { t } = useTranslation();
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [connecting, setConnecting] = useState<string | null>(null);

  const loadConnectors = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listAvailableConnectors();
      setConnectors(data);
    } catch {
      // Silent fail — show empty
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    if (open) loadConnectors();
  }, [open, loadConnectors]);

  const handleConnect = async (provider: string) => {
    setConnecting(provider);
    try {
      const { auth_url } = await initiateOAuth(provider);
      // Open OAuth in popup window
      const popup = window.open(auth_url, `connect-${provider}`, "width=600,height=700");

      // Poll for popup close (OAuth callback redirects back)
      const interval = setInterval(() => {
        if (popup?.closed) {
          clearInterval(interval);
          setConnecting(null);
          loadConnectors(); // Refresh state
        }
      }, 500);

      // Cleanup after 5 minutes
      setTimeout(() => {
        clearInterval(interval);
        setConnecting(null);
      }, 300000);
    } catch (err: any) {
      alert(err.response?.data?.detail || err.message);
      setConnecting(null);
    }
  };

  const handleDisconnect = async (provider: string) => {
    try {
      await disconnectConnector(provider);
      loadConnectors();
    } catch (err: any) {
      alert(err.response?.data?.detail || err.message);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
      <div className="bg-white dark:bg-zinc-900 rounded-xl shadow-2xl w-[520px] max-h-[80vh] overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-700">
          <div>
            <h2 className="text-lg font-semibold">
              {t("connectors.title", "连接器")}
            </h2>
            <p className="text-sm text-zinc-500">
              {t("connectors.subtitle", "连接第三方服务以增强 AI 能力")}
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-2 rounded-lg hover:bg-zinc-100 dark:hover:bg-zinc-800 transition"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M4 4L12 12M12 4L4 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
            </svg>
          </button>
        </div>

        {/* Connector List */}
        <div className="px-6 py-4 space-y-3 overflow-y-auto max-h-[60vh]">
          {loading ? (
            <div className="text-center py-8 text-zinc-500">Loading...</div>
          ) : connectors.length === 0 ? (
            <div className="text-center py-8 text-zinc-500">
              {t("connectors.none", "暂无可用连接器。请联系管理员配置 OAuth 凭据。")}
            </div>
          ) : (
            connectors.map((c) => (
              <ConnectorRow
                key={c.provider}
                connector={c}
                connecting={connecting === c.provider}
                onConnect={() => handleConnect(c.provider)}
                onDisconnect={() => handleDisconnect(c.provider)}
              />
            ))
          )}
        </div>

        {/* Footer */}
        <div className="px-6 py-3 border-t border-zinc-200 dark:border-zinc-700 text-xs text-zinc-400">
          {t("connectors.footer", "连接后，AI 可以搜索和引用您的第三方服务数据。令牌加密存储。")}
        </div>
      </div>
    </div>
  );
}

function ConnectorRow({
  connector,
  connecting,
  onConnect,
  onDisconnect,
}: {
  connector: ConnectorInfo;
  connecting: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
}) {
  const icon = PROVIDER_ICONS[connector.provider] ?? "🔗";

  return (
    <div className="flex items-center gap-4 p-3 rounded-lg border border-zinc-200 dark:border-zinc-700 hover:border-zinc-300 dark:hover:border-zinc-600 transition">
      {/* Icon */}
      <div className="text-2xl w-10 h-10 flex items-center justify-center rounded-lg bg-zinc-100 dark:bg-zinc-800">
        {icon}
      </div>

      {/* Info */}
      <div className="flex-1 min-w-0">
        <div className="font-medium text-sm">{connector.display_name}</div>
        <div className="text-xs text-zinc-500 truncate">{connector.description}</div>
      </div>

      {/* Status + Action */}
      <div className="flex items-center gap-2">
        {connector.connected ? (
          <>
            <span className="w-2 h-2 rounded-full bg-green-500" />
            <button
              onClick={onDisconnect}
              className="px-3 py-1.5 text-xs font-medium text-red-600 bg-red-50 dark:bg-red-900/20 dark:text-red-400 rounded-lg hover:bg-red-100 dark:hover:bg-red-900/40 transition"
            >
              断开
            </button>
          </>
        ) : (
          <button
            onClick={onConnect}
            disabled={connecting}
            className="px-3 py-1.5 text-xs font-medium text-blue-600 bg-blue-50 dark:bg-blue-900/20 dark:text-blue-400 rounded-lg hover:bg-blue-100 dark:hover:bg-blue-900/40 transition disabled:opacity-50"
          >
            {connecting ? "连接中..." : "连接"}
          </button>
        )}
      </div>
    </div>
  );
}
