import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";

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

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources,
    fallbackLng: DEFAULT_LOCALE,
    supportedLngs: [...APP_LOCALES],
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
    detection: {
      // Do NOT include "navigator" — browser system language (often zh-CN
      // for Chinese users) would override the en-US default on every fresh
      // visit, even after clearing localStorage.  With this order:
      // - First visit: no localStorage → fallback to en-US ✓
      // - User switches language in UI → saved to localStorage ✓
      // - Next visit: reads from localStorage ✓
      order: ["localStorage", "htmlTag"],
      caches: ["localStorage"],
      lookupLocalStorage: "i18nextLng",
      convertDetectedLanguage: (lng) => resolveAppLocale(lng),
    },
  });

export default i18n;

// 支持的语言列表
export const languages = [
  { code: "zh-CN", nameKey: "language.zhCN", nativeName: "简体中文", flag: "🇨🇳" },
  { code: "en-US", nameKey: "language.enUS", nativeName: "English", flag: "🇺🇸" },
] as const;

export type LanguageCode = (typeof languages)[number]["code"];
