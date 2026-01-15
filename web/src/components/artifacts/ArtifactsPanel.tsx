import * as React from "react";
import {
  X,
  Copy,
  Check,
  Download,
  Code2,
  FileText,
  BarChart3,
  Terminal,
  RefreshCw,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ExecutionStatus, type ExecutionStatusType } from "./ExecutionStatus";

export interface Artifact {
  id: string;
  type: "code" | "chart" | "table" | "file";
  format: string;
  title: string;
  url?: string;
  content?: string;
  createdAt: Date;
}

interface ArtifactsPanelProps {
  isOpen: boolean;
  onClose: () => void;
  artifacts: Artifact[];
  executionStatus: ExecutionStatusType;
  executionOutput: string;
  currentCode?: string;
  executionTimeMs?: number;
  onRerun?: () => void;
  className?: string;
}

export function ArtifactsPanel({
  isOpen,
  onClose,
  artifacts,
  executionStatus,
  executionOutput,
  currentCode,
  executionTimeMs,
  onRerun,
  className,
}: ArtifactsPanelProps) {
  const [copiedCode, setCopiedCode] = React.useState(false);

  const charts = artifacts.filter((a) => a.type === "chart");
  const files = artifacts.filter((a) => a.type === "file" || a.type === "table");

  const handleCopyCode = React.useCallback(async () => {
    if (!currentCode) return;
    try {
      await navigator.clipboard.writeText(currentCode);
      setCopiedCode(true);
      setTimeout(() => setCopiedCode(false), 2000);
    } catch (err) {
      console.error("Failed to copy code:", err);
    }
  }, [currentCode]);

  if (!isOpen) return null;

  return (
    <div
      className={cn(
        "flex flex-col h-full bg-background border-l border-border",
        className
      )}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-semibold">Artifacts</h2>
          <ExecutionStatus
            status={executionStatus}
            executionTimeMs={executionTimeMs}
          />
        </div>
        <div className="flex items-center gap-2">
          {onRerun && executionStatus !== "running" && (
            <Button
              variant="ghost"
              size="icon"
              onClick={onRerun}
              title="Re-run code"
            >
              <RefreshCw className="h-4 w-4" />
            </Button>
          )}
          <Button variant="ghost" size="icon" onClick={onClose} title="Close">
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* Tabs */}
      <Tabs defaultValue="output" className="flex-1 flex flex-col min-h-0">
        <TabsList className="mx-4 mt-3 justify-start w-fit">
          <TabsTrigger value="output" className="gap-1.5">
            <Terminal className="h-3.5 w-3.5" />
            Output
          </TabsTrigger>
          <TabsTrigger value="code" className="gap-1.5">
            <Code2 className="h-3.5 w-3.5" />
            Code
          </TabsTrigger>
          <TabsTrigger value="charts" className="gap-1.5">
            <BarChart3 className="h-3.5 w-3.5" />
            Charts
            {charts.length > 0 && (
              <span className="ml-1 text-xs text-muted-foreground">
                ({charts.length})
              </span>
            )}
          </TabsTrigger>
          <TabsTrigger value="files" className="gap-1.5">
            <FileText className="h-3.5 w-3.5" />
            Files
            {files.length > 0 && (
              <span className="ml-1 text-xs text-muted-foreground">
                ({files.length})
              </span>
            )}
          </TabsTrigger>
        </TabsList>

        {/* Output Tab */}
        <TabsContent value="output" className="flex-1 min-h-0 m-0 px-4 pb-4">
          <div className="h-full overflow-auto rounded-md bg-muted/50 border border-border">
            <pre className="p-4 text-sm font-mono whitespace-pre-wrap break-words text-foreground">
              {executionOutput || (
                <span className="text-muted-foreground italic">
                  No output yet
                </span>
              )}
            </pre>
          </div>
        </TabsContent>

        {/* Code Tab */}
        <TabsContent value="code" className="flex-1 min-h-0 m-0 px-4 pb-4">
          <div className="h-full flex flex-col">
            <div className="flex justify-end mb-2">
              <Button
                variant="outline"
                size="sm"
                onClick={handleCopyCode}
                disabled={!currentCode}
                className="gap-1.5"
              >
                {copiedCode ? (
                  <>
                    <Check className="h-3.5 w-3.5" />
                    Copied
                  </>
                ) : (
                  <>
                    <Copy className="h-3.5 w-3.5" />
                    Copy
                  </>
                )}
              </Button>
            </div>
            <div className="flex-1 overflow-auto rounded-md bg-muted/50 border border-border">
              <pre className="p-4 text-sm font-mono whitespace-pre-wrap break-words text-foreground">
                {currentCode || (
                  <span className="text-muted-foreground italic">
                    No code available
                  </span>
                )}
              </pre>
            </div>
          </div>
        </TabsContent>

        {/* Charts Tab */}
        <TabsContent value="charts" className="flex-1 min-h-0 m-0 px-4 pb-4">
          <div className="h-full overflow-auto">
            {charts.length === 0 ? (
              <div className="flex items-center justify-center h-full text-muted-foreground">
                <p className="text-sm italic">No charts generated</p>
              </div>
            ) : (
              <div className="space-y-4">
                {charts.map((chart) => (
                  <div
                    key={chart.id}
                    className="rounded-md border border-border bg-card overflow-hidden"
                  >
                    <div className="px-3 py-2 border-b border-border bg-muted/30">
                      <h3 className="text-sm font-medium">{chart.title}</h3>
                      <p className="text-xs text-muted-foreground">
                        {chart.format}
                      </p>
                    </div>
                    <div className="p-4">
                      {chart.url ? (
                        <img
                          src={chart.url}
                          alt={chart.title}
                          className="max-w-full h-auto"
                        />
                      ) : chart.content ? (
                        <div
                          dangerouslySetInnerHTML={{ __html: chart.content }}
                        />
                      ) : (
                        <p className="text-sm text-muted-foreground italic">
                          Chart content unavailable
                        </p>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </TabsContent>

        {/* Files Tab */}
        <TabsContent value="files" className="flex-1 min-h-0 m-0 px-4 pb-4">
          <div className="h-full overflow-auto">
            {files.length === 0 ? (
              <div className="flex items-center justify-center h-full text-muted-foreground">
                <p className="text-sm italic">No files generated</p>
              </div>
            ) : (
              <div className="space-y-2">
                {files.map((file) => (
                  <div
                    key={file.id}
                    className="flex items-center justify-between p-3 rounded-md border border-border bg-card hover:bg-accent/50 transition-colors"
                  >
                    <div className="flex items-center gap-3 min-w-0">
                      <FileText className="h-5 w-5 text-muted-foreground flex-shrink-0" />
                      <div className="min-w-0">
                        <p className="text-sm font-medium truncate">
                          {file.title}
                        </p>
                        <p className="text-xs text-muted-foreground">
                          {file.format}
                        </p>
                      </div>
                    </div>
                    {file.url && (
                      <Button
                        variant="ghost"
                        size="sm"
                        asChild
                        className="gap-1.5 flex-shrink-0"
                      >
                        <a
                          href={file.url}
                          download={file.title}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          <Download className="h-3.5 w-3.5" />
                          Download
                        </a>
                      </Button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
