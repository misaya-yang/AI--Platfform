import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import zhCN from "./locales/zh-CN.json";
import enUS from "./locales/en-US.json";

export const APP_LOCALES = ["zh-CN", "en-US"] as const;
export type AppLocale = (typeof APP_LOCALES)[number];

const DEFAULT_LOCALE: AppLocale = "en-US";
const APP_LOCALE_SET = new Set<string>(APP_LOCALES);

export function resolveAppLocale(input?: string): AppLocale {
  if (!input) return DEFAULT_LOCALE;
  if (APP_LOCALE_SET.has(input)) return input as AppLocale;

  const normalized = input.replace("_", "-");
  if (APP_LOCALE_SET.has(normalized)) return normalized as AppLocale;

  const lower = normalized.toLowerCase();
  if (lower.startsWith("zh")) return "zh-CN";
  if (lower.startsWith("en")) return "en-US";
  return DEFAULT_LOCALE;
}

const resources = {
  "zh-CN": {
    translation: zhCN,
  },
  "en-US": {
    translation: enUS,
  },
};

// Deterministic language init — NO LanguageDetector.
// LanguageDetector has too many implicit sources (navigator, htmlTag, querystring)
// that override the intended default. Instead: read localStorage directly.
const savedLng = typeof window !== "undefined"
  ? localStorage.getItem("i18nextLng")
  : null;
const initialLng = savedLng && APP_LOCALE_SET.has(savedLng)
  ? savedLng as AppLocale
  : DEFAULT_LOCALE;

i18n
  .use(initReactI18next)
  .init({
    lng: initialLng,
    resources,
    fallbackLng: DEFAULT_LOCALE,
    supportedLngs: [...APP_LOCALES],
    debug: false,
    showSupportNotice: false,
    nonExplicitSupportedLngs: false,
    load: "currentOnly",
    cleanCode: true,
    returnNull: false,
    returnEmptyString: false,
    appendNamespaceToMissingKey: false,
    saveMissing: false,
    interpolation: {
      escapeValue: false,
    },
  });

// When user switches language via UI, persist to localStorage so it sticks.
i18n.on("languageChanged", (lng) => {
  if (typeof window !== "undefined") {
    localStorage.setItem("i18nextLng", lng);
    document.documentElement.lang = lng;
  }
});

export default i18n;

// 支持的语言列表
export const languages = [
  { code: "zh-CN", nameKey: "language.zhCN", nativeName: "简体中文", flag: "🇨🇳" },
  { code: "en-US", nameKey: "language.enUS", nativeName: "English", flag: "🇺🇸" },
] as const;

export type LanguageCode = (typeof languages)[number]["code"];
