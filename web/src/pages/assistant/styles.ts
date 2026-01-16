/**
 * Assistant Style Presets
 *
 * Grok-inspired personality styles that modify the assistant's response behavior.
 * Each style adds a system prompt prefix to guide the model's responses.
 */

export interface AssistantStyle {
  id: string;
  name: string;
  nameZh: string;
  description: string;
  descriptionZh: string;
  systemPrompt: string | null;
}

export const ASSISTANT_STYLES: AssistantStyle[] = [
  {
    id: "custom",
    name: "Custom",
    nameZh: "自定义",
    description: "Responds to you as you please.",
    descriptionZh: "根据您的喜好回应。",
    systemPrompt: null,
  },
  {
    id: "concise",
    name: "Concise",
    nameZh: "简洁模式",
    description: "Provides short and direct responses.",
    descriptionZh: "提供简短直接的回应。",
    systemPrompt: "提供简短直接的回应，避免冗余。保持简洁明了，直击要点。",
  },
  {
    id: "formal",
    name: "Formal",
    nameZh: "正式模式",
    description: "Uses formal language to respond.",
    descriptionZh: "使用正式语气回答。",
    systemPrompt: "使用正式语气回答，保持专业严谨。避免口语化表达，措辞得体。",
  },
  {
    id: "socratic",
    name: "Socratic",
    nameZh: "苏格拉底模式",
    description: "Responds in a guided learning style.",
    descriptionZh: "以引导学习的方式回答。",
    systemPrompt: "以引导学习的方式回答，通过提问引导用户思考，而不是直接给出答案。帮助用户自己发现答案。",
  },
  {
    id: "comprehensive",
    name: "Comprehensive",
    nameZh: "详尽模式",
    description: "Responds with thorough explanations.",
    descriptionZh: "提供详尽的解释。",
    systemPrompt: "提供详尽的解释和全面的分析。包含相关背景知识、多角度分析和具体示例。",
  },
];

export const DEFAULT_STYLE_ID = "custom";

/**
 * Get style by ID
 */
export function getStyleById(id: string): AssistantStyle | undefined {
  return ASSISTANT_STYLES.find((style) => style.id === id);
}

/**
 * Get the system prompt for a style
 */
export function getStyleSystemPrompt(styleId: string): string | null {
  const style = getStyleById(styleId);
  return style?.systemPrompt ?? null;
}
