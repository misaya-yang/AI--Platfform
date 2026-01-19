import { useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { 
  generateImage, 
  getArtifactDownloadUrl,
  createArtifact 
} from "@/api/assistant";
import { addSessionMessage } from "@/api/sessions";
import type { ChatMessage } from "../types";
import type { Artifact } from "@/components/artifacts";

export function useImageGeneration(
  activeSessionId: string | null,
  selectedModel: string,
  setMessages: React.Dispatch<React.SetStateAction<ChatMessage[]>>,
  setArtifacts: React.Dispatch<React.SetStateAction<Artifact[]>>,
  setActiveSessionId: (id: string) => void,
  createSession: (params: any) => Promise<any>,
  listSessions: (params: any) => Promise<any>,
  setSessions: (sessions: any[]) => void,
  config: any
) {
  const { t } = useTranslation();
  const [isImageMode, setIsImageMode] = useState(false);
  const [isGeneratingImage, setIsGeneratingImage] = useState(false);

  const handleImageGenerate = useCallback(() => {
    setIsImageMode(true);
  }, []);

  const cancelImageMode = useCallback(() => {
    setIsImageMode(false);
  }, []);

  const sendImageGeneration = useCallback(async (prompt: string, style: string) => {
    if (!prompt.trim() || isGeneratingImage || !selectedModel) return;

    setIsGeneratingImage(true);
    setIsImageMode(false);

    // Create session if needed (Reuse logic from chat session ideally, but duplicated here for decoupling)
    let sessionId = activeSessionId;
    if (!sessionId) {
      try {
        const { session_id } = await createSession({
          service_id: "__builtin_assistant__",  // 保留的 service_id
          metadata: { title: `🎨 ${prompt.slice(0, 40)}...` },
          config: {
            selected_model: selectedModel,
            selected_style: style,
            ...config
          },
        });
        sessionId = session_id;
        setActiveSessionId(sessionId);
        const updatedSessions = await listSessions({ service_id: "__builtin_assistant__", limit: 100 });
        setSessions(updatedSessions);
      } catch (error) {
        console.error("Failed to create session:", error);
      }
    }

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: `🎨 ${t("assistant.generateImagePrompt", "Generate image")}: ${prompt}`,
    };

    const assistantMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: "assistant",
      content: "",
      isGeneratingImage: true,
      imageGenerationPrompt: prompt,
    };

    setMessages((prev) => [...prev, userMessage, assistantMessage]);

    // Save user message
    if (sessionId) {
      addSessionMessage(sessionId, { role: "user", content: userMessage.content }).catch(console.error);
    }

    try {
      const result = await generateImage({
        prompt,
        model_id: selectedModel,
        n: 1,
        style: style === "default" ? undefined : style
      });

      if (result.success && result.images.length > 0) {
        const providerName = result.provider === "google" ? "Gemini" : "DashScope Wanx";
        const artifactUrls: string[] = [];

        for (let i = 0; i < result.images.length; i++) {
          const img = result.images[i];
          if (img.url.startsWith("data:") && sessionId) {
            try {
              const match = img.url.match(/^data:image\/(\w+);base64,(.+)$/);
              if (match) {
                const format = match[1];
                const base64Data = match[2];
                const artifact = await createArtifact({
                  session_id: sessionId,
                  type: "image",
                  format: format,
                  title: `${t("assistant.generatedImage", "Generated Image")} ${i + 1}: ${prompt.slice(0, 30)}...`,
                  filename: `generated_image_${Date.now()}_${i + 1}.${format}`,
                  content_base64: base64Data,
                  source: "image_generation",
                  metadata: { prompt, provider: result.provider, duration_ms: result.duration_ms },
                });
                const url = artifact.download_url || getArtifactDownloadUrl(artifact.artifact_id);
                artifactUrls.push(url);

                setArtifacts((prev) => [...prev, {
                  id: artifact.artifact_id,
                  type: "image",
                  format: artifact.format,
                  title: artifact.title,
                  url: url,
                  filename: artifact.filename,
                  mimeType: artifact.mime_type,
                  sizeBytes: artifact.size_bytes,
                  source: "ai",
                  createdAt: new Date(),
                }]);
              }
            } catch (error) {
              console.error("Failed to save artifact:", error);
              artifactUrls.push(img.url);
            }
          } else {
            artifactUrls.push(img.url);
          }
        }

        const responseContent = artifactUrls.map((url, i) => `![${t("assistant.generatedImage", "Generated Image")} ${i + 1}](${url})`).join("\n\n") + 
          `\n\n*${t("assistant.generatedWith", "Generated with")} ${providerName} (${(result.duration_ms / 1000).toFixed(1)}s)*`;

        setMessages((prev) => prev.map((m) => m.id === assistantMessage.id ? { 
          ...m, content: responseContent, isGeneratingImage: false, imageGenerationPrompt: undefined 
        } : m));

        if (sessionId) {
          addSessionMessage(sessionId, { 
            role: "assistant", 
            content: responseContent,
            metadata: { model_id: selectedModel, provider: result.provider, duration_ms: result.duration_ms }
          }).catch(console.error);
        }

      } else {
        throw new Error(result.error || "Unknown error");
      }
    } catch (error: any) {
      const errorContent = `**${t("assistant.error", "Error")}:** ${error.message}`;
      setMessages((prev) => prev.map((m) => m.id === assistantMessage.id ? { 
        ...m, content: errorContent, isGeneratingImage: false, imageGenerationPrompt: undefined 
      } : m));
      
      if (sessionId) {
        addSessionMessage(sessionId, { role: "assistant", content: errorContent }).catch(console.error);
      }
    } finally {
      setIsGeneratingImage(false);
    }
  }, [isGeneratingImage, selectedModel, activeSessionId, config, t, setMessages, setArtifacts, setActiveSessionId, setSessions, createSession, listSessions]);

  return {
    isImageMode,
    isGeneratingImage,
    handleImageGenerate,
    cancelImageMode,
    sendImageGeneration
  };
}
