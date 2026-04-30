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
import {
  listConnections,
  createConnection,
  testConnection,
  deleteConnection,
} from "@/api/confluence";
import type { ConfluenceConnection } from "@/types/confluence";
import { api } from "@/lib/api";

interface ConnectorsPanelProps {
  open: boolean;
  onClose: () => void;
}

interface ConfluenceMcpStatus {
  mcp_active?: boolean;
  tools?: Array<{ name: string; description: string }>;
}

interface ConfluenceActivateResponse extends ConfluenceMcpStatus {
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

export default function ConnectorsPanel({ open, onClose }: ConnectorsPanelProps) {
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
  const [mcpActive, setMcpActive] = useState(false);
  const [mcpTools, setMcpTools] = useState<Array<{ name: string; description: string }>>([]);
  const [activating, setActivating] = useState(false);

  const loadConnections = useCallback(async () => {
    setLoading(true);
    try {
      const data = await listConnections();
      setConnections(data);
      // Check MCP status
      if (data.some((c) => c.status === "active")) {
        try {
          const { data: status } = await api.get<ConfluenceMcpStatus>("/api/v1/connectors/confluence/mcp-status");
          setMcpActive(Boolean(status.mcp_active));
          setMcpTools(status.tools || []);
        } catch { /* ignore */ }
      }
    } catch {
      // No connections yet
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => {
      void loadConnections();
    }, 0);
    return () => window.clearTimeout(timer);
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
      void loadConnections();
    } catch (err: unknown) {
      setFormError(apiErrorMessage(err, "连接失败"));
    }
    setFormSubmitting(false);
  };

  const handleDelete = async (id: string) => {
    if (!confirm("确定要删除此连接？")) return;
    try {
      await deleteConnection(id);
      void loadConnections();
    } catch { /* ignore */ }
  };

  const handleTest = async (id: string) => {
    try {
      const result = await testConnection(id);
      alert(result.status === "success" ? "✅ 连接正常" : `❌ ${result.message}`);
    } catch (err: unknown) {
      alert(`测试失败: ${apiErrorMessage(err, "未知错误")}`);
    }
  };

  const handleActivateMCP = async () => {
    setActivating(true);
    try {
      const { data } = await api.post<ConfluenceActivateResponse>("/api/v1/connectors/confluence/activate");
      setMcpActive(true);
      setMcpTools(data.tools || []);
      alert(`✅ AI 工具已激活！发现 ${data.tool_count ?? data.tools?.length ?? 0} 个 Confluence 工具`);
    } catch (err: unknown) {
      alert(`激活失败: ${apiErrorMessage(err, "未知错误")}`);
    }
    setActivating(false);
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
                  <div key={conn.connection_id} className="rounded-lg border border-zinc-200 dark:border-zinc-700 mb-2 overflow-hidden">
                    <div className="flex items-center gap-3 p-3">
                      <div className={`w-2 h-2 rounded-full ${conn.status === "active" ? "bg-green-500" : conn.status === "error" ? "bg-red-500" : "bg-zinc-400"}`} />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm font-medium truncate">{conn.name}</div>
                        <div className="text-xs text-zinc-500 truncate">{conn.domain} · {conn.email}</div>
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

                    {/* MCP Activation Bar */}
                    {conn.status === "active" && (
                      <div className={`px-3 py-2 border-t ${mcpActive ? "bg-green-50 dark:bg-green-900/10 border-green-200 dark:border-green-800" : "bg-amber-50 dark:bg-amber-900/10 border-amber-200 dark:border-amber-800"}`}>
                        {mcpActive ? (
                          <div>
                            <div className="flex items-center gap-2 text-xs text-green-700 dark:text-green-400">
                              <span className="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse" />
                              AI 工具已激活 · {mcpTools.length} 个工具可用
                            </div>
                            {mcpTools.length > 0 && (
                              <div className="mt-1 text-[10px] text-green-600/70 dark:text-green-400/60 truncate">
                                {mcpTools.slice(0, 5).map(t => t.name).join(", ")}{mcpTools.length > 5 ? ` +${mcpTools.length - 5} more` : ""}
                              </div>
                            )}
                          </div>
                        ) : (
                          <div className="flex items-center justify-between">
                            <span className="text-xs text-amber-700 dark:text-amber-400">
                              已连接但 AI 工具未激活
                            </span>
                            <button
                              onClick={handleActivateMCP}
                              disabled={activating}
                              className="px-3 py-1 text-xs font-medium text-white bg-violet-600 hover:bg-violet-700 rounded-lg transition disabled:opacity-50"
                            >
                              {activating ? "激活中..." : "激活 AI 工具"}
                            </button>
                          </div>
                        )}
                      </div>
                    )}
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
