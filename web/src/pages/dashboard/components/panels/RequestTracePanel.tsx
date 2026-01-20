// web/src/pages/dashboard/components/panels/RequestTracePanel.tsx

import { Input, Empty, Tag, Spin, Button } from "antd";
import { SearchOutlined, ReloadOutlined, ClockCircleOutlined, CheckCircleOutlined, ExclamationCircleOutlined } from "@ant-design/icons";
import { useState, useCallback } from "react";
import { PanelWrapper } from "../PanelWrapper";
import { useAppStore } from "@/store/useAppStore";
import { getColors } from "../../styles";

// Mock trace data - will be replaced with real API data
interface TraceSpan {
  name: string;
  duration_ms: number;
  status: "success" | "error" | "pending";
  start_offset_ms: number;
}

interface TraceData {
  request_id: string;
  timestamp: string;
  total_duration_ms: number;
  status: "success" | "error";
  model: string;
  service: string;
  spans: TraceSpan[];
}

// Mock trace for demo purposes
const mockTraces: Record<string, TraceData> = {
  "req-demo-001": {
    request_id: "req-demo-001",
    timestamp: new Date().toISOString(),
    total_duration_ms: 1250,
    status: "success",
    model: "gpt-4",
    service: "chat-service",
    spans: [
      { name: "认证验证", duration_ms: 15, status: "success", start_offset_ms: 0 },
      { name: "速率限制检查", duration_ms: 8, status: "success", start_offset_ms: 15 },
      { name: "请求转换", duration_ms: 12, status: "success", start_offset_ms: 23 },
      { name: "上游请求", duration_ms: 1180, status: "success", start_offset_ms: 35 },
      { name: "响应处理", duration_ms: 25, status: "success", start_offset_ms: 1215 },
      { name: "日志记录", duration_ms: 10, status: "success", start_offset_ms: 1240 },
    ],
  },
  "req-demo-002": {
    request_id: "req-demo-002",
    timestamp: new Date(Date.now() - 300000).toISOString(),
    total_duration_ms: 450,
    status: "error",
    model: "claude-3",
    service: "assistant-service",
    spans: [
      { name: "认证验证", duration_ms: 12, status: "success", start_offset_ms: 0 },
      { name: "速率限制检查", duration_ms: 5, status: "success", start_offset_ms: 12 },
      { name: "请求转换", duration_ms: 10, status: "success", start_offset_ms: 17 },
      { name: "上游请求", duration_ms: 400, status: "error", start_offset_ms: 27 },
    ],
  },
};

function formatDuration(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)}s`;
  return `${ms}ms`;
}

function TraceTimeline({ trace }: { trace: TraceData }) {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const maxDuration = trace.total_duration_ms;

  return (
    <div style={{ marginTop: 16 }}>
      {/* Header info */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
          padding: "12px 16px",
          background: colors.innerBg,
          borderRadius: 8,
          border: `1px solid ${colors.border}`,
        }}
      >
        <div>
          <div style={{ fontSize: 13, fontWeight: 600, color: colors.textPrimary }}>
            {trace.request_id}
          </div>
          <div style={{ fontSize: 11, color: colors.textMuted, marginTop: 2 }}>
            {new Date(trace.timestamp).toLocaleString()}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Tag color={trace.status === "success" ? "success" : "error"}>
            {trace.status === "success" ? "成功" : "失败"}
          </Tag>
          <Tag>{trace.model}</Tag>
          <Tag color="blue">{trace.service}</Tag>
        </div>
      </div>

      {/* Visual timeline bar */}
      <div
        style={{
          position: "relative",
          height: 24,
          background: colors.innerBg,
          borderRadius: 4,
          marginBottom: 16,
          overflow: "hidden",
          border: `1px solid ${colors.border}`,
        }}
      >
        {trace.spans.map((span, index) => {
          const left = (span.start_offset_ms / maxDuration) * 100;
          const width = (span.duration_ms / maxDuration) * 100;
          const bgColor =
            span.status === "success"
              ? "rgba(16, 185, 129, 0.7)"
              : span.status === "error"
              ? "rgba(239, 68, 68, 0.7)"
              : "rgba(245, 158, 11, 0.7)";

          return (
            <div
              key={index}
              title={`${span.name}: ${formatDuration(span.duration_ms)}`}
              style={{
                position: "absolute",
                left: `${left}%`,
                width: `${Math.max(width, 1)}%`,
                height: "100%",
                background: bgColor,
                borderRight: "1px solid rgba(255,255,255,0.3)",
              }}
            />
          );
        })}
      </div>

      {/* Detailed span list */}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {trace.spans.map((span, index) => (
          <div
            key={index}
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              padding: "8px 12px",
              background: colors.innerBg,
              borderRadius: 6,
              border: `1px solid ${colors.border}`,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              {span.status === "success" ? (
                <CheckCircleOutlined style={{ color: "#10b981", fontSize: 14 }} />
              ) : span.status === "error" ? (
                <ExclamationCircleOutlined style={{ color: "#ef4444", fontSize: 14 }} />
              ) : (
                <ClockCircleOutlined style={{ color: "#f59e0b", fontSize: 14 }} />
              )}
              <span style={{ fontSize: 12, color: colors.textPrimary }}>{span.name}</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <span style={{ fontSize: 11, color: colors.textMuted }}>
                +{formatDuration(span.start_offset_ms)}
              </span>
              <span
                style={{
                  fontSize: 12,
                  fontWeight: 600,
                  color: span.duration_ms > 100 ? "#f59e0b" : colors.textSecondary,
                  minWidth: 60,
                  textAlign: "right",
                }}
              >
                {formatDuration(span.duration_ms)}
              </span>
            </div>
          </div>
        ))}
      </div>

      {/* Total duration */}
      <div
        style={{
          marginTop: 12,
          padding: "8px 12px",
          background: darkMode ? "rgba(59, 130, 246, 0.1)" : "rgba(59, 130, 246, 0.05)",
          borderRadius: 6,
          border: `1px solid ${darkMode ? "rgba(59, 130, 246, 0.3)" : "rgba(59, 130, 246, 0.2)"}`,
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
        }}
      >
        <span style={{ fontSize: 12, color: colors.textSecondary }}>总耗时</span>
        <span style={{ fontSize: 14, fontWeight: 700, color: "#3b82f6" }}>
          {formatDuration(trace.total_duration_ms)}
        </span>
      </div>
    </div>
  );
}

export function RequestTracePanel() {
  const { darkMode } = useAppStore();
  const colors = getColors(darkMode);
  const [searchId, setSearchId] = useState("");
  const [loading, setLoading] = useState(false);
  const [trace, setTrace] = useState<TraceData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleSearch = useCallback(() => {
    if (!searchId.trim()) return;

    setLoading(true);
    setError(null);

    // Simulate API call - replace with real API when available
    setTimeout(() => {
      const foundTrace = mockTraces[searchId.trim()];
      if (foundTrace) {
        setTrace(foundTrace);
        setError(null);
      } else {
        setTrace(null);
        setError("未找到该请求ID的追踪记录");
      }
      setLoading(false);
    }, 500);
  }, [searchId]);

  const handleReset = () => {
    setSearchId("");
    setTrace(null);
    setError(null);
  };

  return (
    <PanelWrapper title="请求追踪">
      {/* Search bar */}
      <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
        <Input
          placeholder="输入 Request ID 搜索..."
          prefix={<SearchOutlined style={{ color: colors.textMuted }} />}
          value={searchId}
          onChange={(e) => setSearchId(e.target.value)}
          onPressEnter={handleSearch}
          style={{ flex: 1 }}
        />
        <Button type="primary" icon={<SearchOutlined />} onClick={handleSearch} loading={loading}>
          搜索
        </Button>
        {(trace || error) && (
          <Button icon={<ReloadOutlined />} onClick={handleReset}>
            重置
          </Button>
        )}
      </div>

      {/* Demo hint */}
      {!trace && !error && !loading && (
        <div
          style={{
            padding: "8px 12px",
            marginBottom: 16,
            background: darkMode ? "rgba(139, 92, 246, 0.1)" : "rgba(139, 92, 246, 0.05)",
            borderRadius: 6,
            border: `1px solid ${darkMode ? "rgba(139, 92, 246, 0.3)" : "rgba(139, 92, 246, 0.2)"}`,
            fontSize: 12,
            color: "#8b5cf6",
          }}
        >
          演示模式：尝试搜索 "req-demo-001" 或 "req-demo-002" 查看示例追踪数据
        </div>
      )}

      {/* Content */}
      {loading ? (
        <div style={{ textAlign: "center", padding: 40 }}>
          <Spin tip="正在查询追踪记录..." />
        </div>
      ) : trace ? (
        <TraceTimeline trace={trace} />
      ) : error ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <span style={{ color: colors.textMuted }}>{error}</span>
          }
        />
      ) : (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            <div style={{ color: colors.textMuted }}>
              <p>输入 Request ID 查看请求链路追踪</p>
              <p style={{ fontSize: 12, marginTop: 4 }}>支持查看完整请求调用链和各阶段耗时分解</p>
            </div>
          }
        />
      )}
    </PanelWrapper>
  );
}
