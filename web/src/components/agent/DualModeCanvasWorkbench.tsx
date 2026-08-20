import React, { useEffect, useState } from "react";
import {
  Button,
  Tag,
  Space,
  Tooltip,
} from "antd";
import {
  Code,
  FileText,
  Layout,
  Maximize2,
  Copy,
  Check,
  Download,
  Terminal,
  Sparkles,
} from "lucide-react";

export interface ArtifactItem {
  id: string;
  title: string;
  type: "code" | "markdown" | "html" | "json";
  language?: string;
  content: string;
  version?: number;
  timestamp?: string;
}

export interface DualModeCanvasWorkbenchProps {
  chatSlot: React.ReactNode;
  artifacts?: ArtifactItem[];
  activeArtifactId?: string;
  onSelectArtifact?: (id: string) => void;
  className?: string;
}

export const DualModeCanvasWorkbench: React.FC<DualModeCanvasWorkbenchProps> = ({
  chatSlot,
  artifacts = [],
  activeArtifactId,
  onSelectArtifact,
  className = "",
}) => {
  const [selectedId, setSelectedId] = useState<string>(
    activeArtifactId || (artifacts.length > 0 ? artifacts[0].id : "")
  );
  const [splitMode, setSplitMode] = useState<"dual" | "chat_only" | "canvas_only">("dual");
  const [copied, setCopied] = useState<boolean>(false);

  useEffect(() => {
    if (activeArtifactId) {
      setSelectedId(activeArtifactId);
    }
  }, [activeArtifactId]);

  const activeArtifact = artifacts.find((a) => a.id === (activeArtifactId || selectedId)) || artifacts[0];

  const handleCopy = () => {
    if (activeArtifact?.content) {
      navigator.clipboard.writeText(activeArtifact.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const handleDownload = () => {
    if (!activeArtifact) return;
    const blob = new Blob([activeArtifact.content], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${activeArtifact.title || "artifact"}.${activeArtifact.language || "txt"}`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const hasArtifacts = artifacts.length > 0;

  return (
    <div className={`flex flex-col h-full w-full bg-slate-900 text-slate-100 rounded-lg overflow-hidden border border-slate-700 ${className}`}>
      {/* Top Bar for Dual-Mode Canvas Switcher */}
      <div className="flex items-center justify-between px-4 py-2 bg-slate-950 border-b border-slate-800">
        <div className="flex items-center space-x-2">
          <Sparkles className="w-4 h-4 text-indigo-400" />
          <span className="font-semibold text-sm text-slate-200">
            2026 SOTA Agent Workbench
          </span>
          <Tag color="geekblue" className="text-xs">Dual-Mode Canvas</Tag>
        </div>

        <Space size="small">
          <Button
            size="small"
            type={splitMode === "chat_only" ? "primary" : "default"}
            onClick={() => setSplitMode("chat_only")}
            icon={<Terminal className="w-3.5 h-3.5" />}
          >
            Chat
          </Button>
          <Button
            size="small"
            type={splitMode === "dual" ? "primary" : "default"}
            onClick={() => setSplitMode("dual")}
            icon={<Layout className="w-3.5 h-3.5" />}
            disabled={!hasArtifacts}
          >
            Split Canvas
          </Button>
          <Button
            size="small"
            type={splitMode === "canvas_only" ? "primary" : "default"}
            onClick={() => setSplitMode("canvas_only")}
            icon={<Maximize2 className="w-3.5 h-3.5" />}
            disabled={!hasArtifacts}
          >
            Full Canvas
          </Button>
        </Space>
      </div>

      {/* Main Workspace Body */}
      <div className="flex flex-1 overflow-hidden">
        {/* Left Pane: Chat Stream & Tool Execution */}
        {(splitMode === "dual" || splitMode === "chat_only" || !hasArtifacts) && (
          <div
            className={`flex flex-col h-full border-r border-slate-800 ${
              splitMode === "dual" && hasArtifacts ? "w-1/2" : "w-full"
            }`}
          >
            {chatSlot}
          </div>
        )}

        {/* Right Pane: Live Artifacts Canvas */}
        {hasArtifacts && (splitMode === "dual" || splitMode === "canvas_only") && (
          <div
            className={`flex flex-col h-full bg-slate-950/80 ${
              splitMode === "dual" ? "w-1/2" : "w-full"
            }`}
          >
            {/* Artifact Tabs Header */}
            <div className="flex items-center justify-between px-3 py-1.5 bg-slate-900 border-b border-slate-800">
              <div className="flex items-center space-x-2 overflow-x-auto">
                {artifacts.map((art) => {
                  const isCurrent = (activeArtifact?.id === art.id);
                  return (
                    <button
                      key={art.id}
                      onClick={() => {
                        setSelectedId(art.id);
                        onSelectArtifact?.(art.id);
                      }}
                      className={`flex items-center space-x-1.5 px-2.5 py-1 rounded text-xs transition-colors ${
                        isCurrent
                          ? "bg-indigo-600 text-white font-medium shadow-sm"
                          : "text-slate-400 hover:text-slate-200 hover:bg-slate-800"
                      }`}
                    >
                      {art.type === "code" ? (
                        <Code className="w-3.5 h-3.5" />
                      ) : (
                        <FileText className="w-3.5 h-3.5" />
                      )}
                      <span>{art.title}</span>
                      {art.version && <span className="opacity-60">v{art.version}</span>}
                    </button>
                  );
                })}
              </div>

              {/* Action Buttons */}
              <div className="flex items-center space-x-1">
                <Tooltip title={copied ? "Copied" : "Copy Content"}>
                  <Button
                    size="small"
                    type="text"
                    onClick={handleCopy}
                    icon={copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5 text-slate-400" />}
                  />
                </Tooltip>
                <Tooltip title="Download">
                  <Button
                    size="small"
                    type="text"
                    onClick={handleDownload}
                    icon={<Download className="w-3.5 h-3.5 text-slate-400" />}
                  />
                </Tooltip>
              </div>
            </div>

            {/* Canvas Body Content */}
            <div className="flex-1 overflow-auto p-4 font-mono text-xs text-slate-300 leading-relaxed bg-slate-950">
              {activeArtifact?.type === "code" ? (
                <pre className="p-3 bg-slate-900 rounded border border-slate-800 overflow-x-auto">
                  <code>{activeArtifact.content}</code>
                </pre>
              ) : activeArtifact?.type === "markdown" ? (
                <div className="prose prose-invert prose-sm max-w-none">
                  <pre className="whitespace-pre-wrap font-sans text-sm">{activeArtifact.content}</pre>
                </div>
              ) : (
                <pre className="p-3 bg-slate-900 rounded border border-slate-800 overflow-x-auto">
                  <code>{activeArtifact?.content}</code>
                </pre>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default DualModeCanvasWorkbench;
