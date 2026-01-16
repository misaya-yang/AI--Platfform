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
  Image as ImageIcon,
  FileType,
  ExternalLink,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ExecutionStatus, type ExecutionStatusType } from "./ExecutionStatus";

export interface Artifact {
  id: string;
  type: "code" | "chart" | "table" | "file" | "image" | "document";
  format: string;
  title: string;
  url?: string;
  content?: string;
  createdAt: Date;
  // Extended fields for persisted artifacts
  filename?: string;
  mimeType?: string;
  sizeBytes?: number;
  source?: "ai" | "user" | "code_execution" | "image_generation" | "document_generation";
}

export interface OutputFile {
  filename: string;
  content_base64: string;
  mime_type: string | null;
  size_bytes: number;
  artifact_id?: string;
}

interface ArtifactsPanelProps {
  isOpen: boolean;
  onClose: () => void;
  artifacts: Artifact[];
  executionStatus: ExecutionStatusType;
  executionOutput: string;
  currentCode?: string;
  executionTimeMs?: number;
  outputFiles?: OutputFile[];
  onRerun?: () => void;
  className?: string;
}

// Helper to format file size
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// Helper to get icon for file type
function getFileIcon(mimeType?: string, format?: string) {
  if (mimeType?.startsWith("image/") || format === "png" || format === "jpg" || format === "jpeg") {
    return <ImageIcon className="h-5 w-5 text-blue-500 flex-shrink-0" />;
  }
  if (mimeType?.includes("pdf") || format === "pdf") {
    return <FileType className="h-5 w-5 text-red-500 flex-shrink-0" />;
  }
  if (mimeType?.includes("word") || format === "docx" || format === "doc") {
    return <FileText className="h-5 w-5 text-blue-600 flex-shrink-0" />;
  }
  if (mimeType?.includes("markdown") || format === "md") {
    return <FileText className="h-5 w-5 text-gray-600 flex-shrink-0" />;
  }
  return <FileText className="h-5 w-5 text-muted-foreground flex-shrink-0" />;
}

// Helper to get source badge
function getSourceBadge(source?: string) {
  if (!source) return null;
  const labels: Record<string, { text: string; className: string }> = {
    code_execution: { text: "Code", className: "bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400" },
    image_generation: { text: "AI Image", className: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400" },
    document_generation: { text: "AI Doc", className: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400" },
    ai: { text: "AI", className: "bg-violet-100 text-violet-700 dark:bg-violet-900/30 dark:text-violet-400" },
    user: { text: "User", className: "bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400" },
  };
  const badge = labels[source];
  if (!badge) return null;
  return (
    <span className={cn("text-[10px] px-1.5 py-0.5 rounded-full font-medium", badge.className)}>
      {badge.text}
    </span>
  );
}

export function ArtifactsPanel({
  isOpen,
  onClose,
  artifacts,
  executionStatus,
  executionOutput,
  currentCode,
  executionTimeMs,
  outputFiles = [],
  onRerun,
  className,
}: ArtifactsPanelProps) {
  const [copiedCode, setCopiedCode] = React.useState(false);

  // Categorize artifacts
  const charts = artifacts.filter((a) => a.type === "chart");
  const images = artifacts.filter((a) => a.type === "image" || a.mimeType?.startsWith("image/"));
  const documents = artifacts.filter((a) =>
    a.type === "document" ||
    a.format === "docx" ||
    a.format === "pdf" ||
    a.format === "md" ||
    a.mimeType?.includes("word") ||
    a.mimeType?.includes("pdf")
  );
  const otherFiles = artifacts.filter((a) =>
    (a.type === "file" || a.type === "table") &&
    !documents.includes(a) &&
    !images.includes(a)
  );

  // Filter output files
  const imageOutputFiles = outputFiles.filter(
    (f) => f.mime_type?.startsWith("image/")
  );
  const otherOutputFiles = outputFiles.filter(
    (f) => !f.mime_type?.startsWith("image/")
  );

  // Count for tabs
  const chartsCount = charts.length + images.length + imageOutputFiles.length;
  const filesCount = documents.length + otherFiles.length + otherOutputFiles.length;

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
            Images
            {chartsCount > 0 && (
              <span className="ml-1 text-xs text-muted-foreground">
                ({chartsCount})
              </span>
            )}
          </TabsTrigger>
          <TabsTrigger value="files" className="gap-1.5">
            <FileText className="h-3.5 w-3.5" />
            Files
            {filesCount > 0 && (
              <span className="ml-1 text-xs text-muted-foreground">
                ({filesCount})
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

        {/* Charts/Images Tab */}
        <TabsContent value="charts" className="flex-1 min-h-0 m-0 px-4 pb-4">
          <div className="h-full overflow-auto">
            {chartsCount === 0 ? (
              <div className="flex items-center justify-center h-full text-muted-foreground">
                <p className="text-sm italic">No images or charts generated</p>
              </div>
            ) : (
              <div className="space-y-4">
                {/* Output images from code execution (base64) */}
                {imageOutputFiles.map((file, index) => (
                  <div
                    key={`output-${index}`}
                    className="rounded-md border border-border bg-card overflow-hidden"
                  >
                    <div className="px-3 py-2 border-b border-border bg-muted/30 flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <div>
                          <h3 className="text-sm font-medium">{file.filename}</h3>
                          <p className="text-xs text-muted-foreground">
                            {file.mime_type || "image"} • {formatFileSize(file.size_bytes)}
                          </p>
                        </div>
                        {getSourceBadge("code_execution")}
                      </div>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="gap-1.5"
                        onClick={() => {
                          const link = document.createElement("a");
                          link.href = `data:${file.mime_type || "image/png"};base64,${file.content_base64}`;
                          link.download = file.filename;
                          link.click();
                        }}
                      >
                        <Download className="h-3.5 w-3.5" />
                        Download
                      </Button>
                    </div>
                    <div className="p-4 flex justify-center bg-muted/20">
                      <img
                        src={`data:${file.mime_type || "image/png"};base64,${file.content_base64}`}
                        alt={file.filename}
                        className="max-w-full h-auto max-h-[500px] object-contain"
                      />
                    </div>
                  </div>
                ))}

                {/* Persisted image artifacts */}
                {images.map((image) => (
                  <div
                    key={image.id}
                    className="rounded-md border border-border bg-card overflow-hidden"
                  >
                    <div className="px-3 py-2 border-b border-border bg-muted/30 flex items-center justify-between">
                      <div className="flex items-center gap-2">
                        <div>
                          <h3 className="text-sm font-medium">{image.title}</h3>
                          <p className="text-xs text-muted-foreground">
                            {image.format} {image.sizeBytes && `• ${formatFileSize(image.sizeBytes)}`}
                          </p>
                        </div>
                        {getSourceBadge(image.source)}
                      </div>
                      {image.url && (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="gap-1.5"
                          asChild
                        >
                          <a href={image.url} download={image.filename || image.title} target="_blank" rel="noopener noreferrer">
                            <Download className="h-3.5 w-3.5" />
                            Download
                          </a>
                        </Button>
                      )}
                    </div>
                    <div className="p-4 flex justify-center bg-muted/20">
                      {image.url ? (
                        <img
                          src={image.url}
                          alt={image.title}
                          className="max-w-full h-auto max-h-[500px] object-contain"
                        />
                      ) : (
                        <p className="text-sm text-muted-foreground italic">
                          Image preview unavailable
                        </p>
                      )}
                    </div>
                  </div>
                ))}

                {/* Chart artifacts */}
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
            {filesCount === 0 ? (
              <div className="flex items-center justify-center h-full text-muted-foreground">
                <p className="text-sm italic">No files generated</p>
              </div>
            ) : (
              <div className="space-y-2">
                {/* Document artifacts (docx, pdf, md) */}
                {documents.map((doc) => (
                  <div
                    key={doc.id}
                    className="flex items-center justify-between p-3 rounded-md border border-border bg-card hover:bg-accent/50 transition-colors"
                  >
                    <div className="flex items-center gap-3 min-w-0">
                      {getFileIcon(doc.mimeType, doc.format)}
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <p className="text-sm font-medium truncate">
                            {doc.filename || doc.title}
                          </p>
                          {getSourceBadge(doc.source)}
                        </div>
                        <p className="text-xs text-muted-foreground">
                          {doc.format.toUpperCase()} {doc.sizeBytes && `• ${formatFileSize(doc.sizeBytes)}`}
                        </p>
                      </div>
                    </div>
                    {doc.url && (
                      <div className="flex items-center gap-1 flex-shrink-0">
                        <Button
                          variant="ghost"
                          size="sm"
                          asChild
                          className="gap-1.5"
                        >
                          <a
                            href={doc.url}
                            download={doc.filename || doc.title}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            <Download className="h-3.5 w-3.5" />
                            Download
                          </a>
                        </Button>
                      </div>
                    )}
                  </div>
                ))}

                {/* Other file artifacts */}
                {otherFiles.map((file) => (
                  <div
                    key={file.id}
                    className="flex items-center justify-between p-3 rounded-md border border-border bg-card hover:bg-accent/50 transition-colors"
                  >
                    <div className="flex items-center gap-3 min-w-0">
                      {getFileIcon(file.mimeType, file.format)}
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <p className="text-sm font-medium truncate">
                            {file.filename || file.title}
                          </p>
                          {getSourceBadge(file.source)}
                        </div>
                        <p className="text-xs text-muted-foreground">
                          {file.format} {file.sizeBytes && `• ${formatFileSize(file.sizeBytes)}`}
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
                          download={file.filename || file.title}
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

                {/* Output files from code execution (non-image) */}
                {otherOutputFiles.map((file, index) => (
                  <div
                    key={`output-file-${index}`}
                    className="flex items-center justify-between p-3 rounded-md border border-border bg-card hover:bg-accent/50 transition-colors"
                  >
                    <div className="flex items-center gap-3 min-w-0">
                      {getFileIcon(file.mime_type || undefined)}
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <p className="text-sm font-medium truncate">
                            {file.filename}
                          </p>
                          {getSourceBadge("code_execution")}
                        </div>
                        <p className="text-xs text-muted-foreground">
                          {file.mime_type || "file"} • {formatFileSize(file.size_bytes)}
                        </p>
                      </div>
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="gap-1.5 flex-shrink-0"
                      onClick={() => {
                        const link = document.createElement("a");
                        link.href = `data:${file.mime_type || "application/octet-stream"};base64,${file.content_base64}`;
                        link.download = file.filename;
                        link.click();
                      }}
                    >
                      <Download className="h-3.5 w-3.5" />
                      Download
                    </Button>
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
