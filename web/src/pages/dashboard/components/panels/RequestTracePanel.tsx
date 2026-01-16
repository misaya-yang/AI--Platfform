// web/src/pages/dashboard/components/panels/RequestTracePanel.tsx

import { Input, Empty } from "antd";
import { SearchOutlined } from "@ant-design/icons";
import { useState } from "react";
import { PanelWrapper } from "../PanelWrapper";
import { useAppStore } from "@/store/useAppStore";

// Note: This is a placeholder panel. Full implementation requires backend trace API.
// For now, we show a message about upcoming feature.

export function RequestTracePanel() {
  const { darkMode } = useAppStore();
  const [searchId, setSearchId] = useState("");

  return (
    <PanelWrapper title="请求追踪">
      {/* Search bar */}
      <Input
        placeholder="搜索 Request ID..."
        prefix={<SearchOutlined style={{ color: darkMode ? "#64748b" : "#94a3b8" }} />}
        value={searchId}
        onChange={(e) => setSearchId(e.target.value)}
        style={{ marginBottom: 16 }}
      />

      {/* Placeholder content */}
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={
          <div style={{ color: darkMode ? "#94a3b8" : "#64748b" }}>
            <p>链路追踪功能开发中...</p>
            <p style={{ fontSize: 12 }}>将支持查看完整请求调用链和耗时分解</p>
          </div>
        }
      />
    </PanelWrapper>
  );
}
