/**
 * ConnectorsPanel — quick access to third-party data sources from Chat.
 *
 * Shows:
 * 1. Confluence connections (existing system — API Token auth)
 * 2. Future: Outlook, GitHub, Gmail (OAuth-based)
 *
 * For Confluence, links to the existing /tasks page for full management,
 * and shows a quick-connect form for new connections.
 */

import { useState, useEffect, useCallback } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  listConnections,
  createConnection,
  testConnection,
  deleteConnection,
} from "@/api/confluence";
import type { ConfluenceConnection } from "@/types/confluence";

interface ConnectorsPanelProps {
  open: boolean;
  onClose: () => void;
}

export default function ConnectorsPanel({ open, onClose }: ConnectorsPanelProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [connections, setConnections] = useState<ConfluenceConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAddForm, setShowAddForm] = useState(false);

  // Quick-add form state
  const [formName, setFormName] = useState("");
  const [formDomain, setFormDomain] = useState("");
  const [formEmail, setFormEmail] = useState("");
  const [formToken, setFormToken] = useState("");
  const [formError, setFormError] = useState("");
  const [formSubmitting, setFormSubmitting] = useState(false);

  const loadConnections = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listConnections();
      setConnections(data);
    } catch {
      // No connections yet
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    if (open) loadConnections();
  }, [open, loadConnections]);

  const handleQuickConnect = async () => {
    if (!formDomain || !formEmail || !formToken) {
      setFormError("请填写所有必填字段");
      return;
    }
    setFormSubmitting(true);
    setFormError("");
    try {
      await createConnection({
        name: formName || formDomain.replace(/\.atlassian\.net$/, ""),
        domain: formDomain.replace(/^https?:\/\//, "").replace(/\/$/, ""),
        email: formEmail,
        api_token: formToken,
      });
      setShowAddForm(false);
      setFormName(""); setFormDomain(""); setFormEmail(""); setFormToken("");
      loadConnections();
    } catch (err: any) {
      setFormError(err?.response?.data?.detail || err?.message || "连接失败");
    }
    setFormSubmitting(false);
  };

  const handleDelete = async (id: string) => {
    if (!confirm("确定要删除此连接？")) return;
    try {
      await deleteConnection(id);
      loadConnections();
    } catch { /* ignore */ }
  };

  const handleTest = async (id: string) => {
    try {
      const result = await testConnection(id);
      alert(result.status === "success" ? "✅ 连接正常" : `❌ ${result.message}`);
    } catch (err: any) {
      alert(`测试失败: ${err?.message}`);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div className="bg-white dark:bg-zinc-900 rounded-xl shadow-2xl w-[560px] max-h-[80vh] overflow-hidden"
           onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-zinc-200 dark:border-zinc-700">
          <div>
            <h2 className="text-lg font-semibold">连接器</h2>
            <p className="text-sm text-zinc-500">连接第三方数据源，AI 可搜索和引用其中内容</p>
          </div>
          <button onClick={onClose} className="p-2 rounded-lg hover:bg-zinc-100 dark:hover:bg-zinc-800 transition">
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M4 4L12 12M12 4L4 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
            </svg>
          </button>
        </div>

        {/* Content */}
        <div className="px-6 py-4 space-y-4 overflow-y-auto max-h-[60vh]">
          {/* ── Confluence Section ── */}
          <div>
            <div className="flex items-center gap-2 mb-3">
              <span className="text-xl">📄</span>
              <span className="font-medium">Confluence</span>
              <span className="text-xs text-zinc-400 ml-auto">
                API Token 认证 · 同步到知识库
              </span>
            </div>

            {loading ? (
              <div className="text-sm text-zinc-500 py-4 text-center">加载中...</div>
            ) : connections.length === 0 && !showAddForm ? (
              <div className="text-center py-4">
                <p className="text-sm text-zinc-500 mb-3">尚未连接 Confluence</p>
                <button
                  onClick={() => setShowAddForm(true)}
                  className="px-4 py-2 text-sm font-medium text-blue-600 bg-blue-50 dark:bg-blue-900/20 dark:text-blue-400 rounded-lg hover:bg-blue-100 transition"
                >
                  + 添加连接
                </button>
              </div>
            ) : (
              <>
                {/* Existing connections */}
                {connections.map((conn) => (
                  <div key={conn.connection_id}
                    className="flex items-center gap-3 p-3 rounded-lg border border-zinc-200 dark:border-zinc-700 mb-2">
                    <div className={`w-2 h-2 rounded-full ${conn.status === "active" ? "bg-green-500" : conn.status === "error" ? "bg-red-500" : "bg-zinc-400"}`} />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium truncate">{conn.name}</div>
                      <div className="text-xs text-zinc-500 truncate">{conn.domain} · {conn.email}</div>
                      {conn.last_sync_at && (
                        <div className="text-xs text-zinc-400">最后同步: {new Date(conn.last_sync_at).toLocaleString()}</div>
                      )}
                    </div>
                    <div className="flex gap-1.5">
                      <button onClick={() => handleTest(conn.connection_id)}
                        className="px-2 py-1 text-xs text-zinc-600 hover:bg-zinc-100 dark:hover:bg-zinc-800 rounded transition">
                        测试
                      </button>
                      <button onClick={() => { navigate(`/tasks`); onClose(); }}
                        className="px-2 py-1 text-xs text-blue-600 hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded transition">
                        管理
                      </button>
                      <button onClick={() => handleDelete(conn.connection_id)}
                        className="px-2 py-1 text-xs text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded transition">
                        删除
                      </button>
                    </div>
                  </div>
                ))}

                {/* Add button */}
                {!showAddForm && (
                  <button onClick={() => setShowAddForm(true)}
                    className="w-full py-2 text-sm text-zinc-500 hover:text-zinc-700 hover:bg-zinc-50 dark:hover:bg-zinc-800 rounded-lg transition">
                    + 添加新连接
                  </button>
                )}
              </>
            )}

            {/* Quick-add form */}
            {showAddForm && (
              <div className="p-4 rounded-lg border border-blue-200 dark:border-blue-800 bg-blue-50/50 dark:bg-blue-900/10 space-y-3">
                <div className="text-sm font-medium text-blue-700 dark:text-blue-300">快速连接 Confluence</div>

                <input placeholder="连接名称 (可选)"
                  value={formName} onChange={(e) => setFormName(e.target.value)}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-800" />

                <input placeholder="Confluence 域名 (如: mycompany.atlassian.net)"
                  value={formDomain} onChange={(e) => setFormDomain(e.target.value)}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-800" />

                <input placeholder="邮箱" type="email"
                  value={formEmail} onChange={(e) => setFormEmail(e.target.value)}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-800" />

                <input placeholder="API Token" type="password"
                  value={formToken} onChange={(e) => setFormToken(e.target.value)}
                  className="w-full px-3 py-2 text-sm rounded-lg border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-800" />

                <div className="text-xs text-zinc-400">
                  获取 API Token: <a href="https://id.atlassian.com/manage-profile/security/api-tokens"
                    target="_blank" rel="noopener" className="text-blue-500 hover:underline">
                    Atlassian API Tokens
                  </a>
                </div>

                {formError && <div className="text-xs text-red-500">{formError}</div>}

                <div className="flex gap-2 justify-end">
                  <button onClick={() => { setShowAddForm(false); setFormError(""); }}
                    className="px-3 py-1.5 text-xs text-zinc-600 hover:bg-zinc-100 dark:hover:bg-zinc-800 rounded-lg transition">
                    取消
                  </button>
                  <button onClick={handleQuickConnect} disabled={formSubmitting}
                    className="px-4 py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 rounded-lg transition disabled:opacity-50">
                    {formSubmitting ? "连接中..." : "连接"}
                  </button>
                </div>
              </div>
            )}
          </div>

          {/* ── Divider ── */}
          <div className="border-t border-zinc-200 dark:border-zinc-700" />

          {/* ── Coming Soon: Other Connectors ── */}
          <div className="space-y-2">
            <div className="text-xs font-medium text-zinc-400 uppercase tracking-wider">即将支持</div>

            {[
              { icon: "📧", name: "Outlook", desc: "搜索和发送邮件" },
              { icon: "🐙", name: "GitHub", desc: "搜索代码和管理 Issues" },
              { icon: "✉️", name: "Gmail", desc: "搜索邮件" },
              { icon: "📁", name: "Google Drive", desc: "搜索文档" },
              { icon: "💬", name: "Slack", desc: "搜索消息和频道" },
            ].map((item) => (
              <div key={item.name} className="flex items-center gap-3 p-2.5 rounded-lg opacity-50">
                <span className="text-lg">{item.icon}</span>
                <div className="flex-1">
                  <div className="text-sm font-medium">{item.name}</div>
                  <div className="text-xs text-zinc-500">{item.desc}</div>
                </div>
                <span className="text-xs text-zinc-400 bg-zinc-100 dark:bg-zinc-800 px-2 py-0.5 rounded">
                  即将推出
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Footer */}
        <div className="px-6 py-3 border-t border-zinc-200 dark:border-zinc-700 flex items-center justify-between">
          <span className="text-xs text-zinc-400">连接后，同步的内容可被 AI 检索引用</span>
          <button onClick={() => { navigate("/tasks"); onClose(); }}
            className="text-xs text-blue-500 hover:underline">
            前往完整管理页面 →
          </button>
        </div>
      </div>
    </div>
  );
}
