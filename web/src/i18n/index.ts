import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";

import zhCN from "./locales/zh-CN.json";
import enUS from "./locales/en-US.json";

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
    fallbackLng: "zh-CN",
    interpolation: {
      escapeValue: false,
    },
    detection: {
      order: ["localStorage", "navigator"],
      caches: ["localStorage"],
    },
  });

export default i18n;

// 支持的语言列表
export const languages = [
  { code: "zh-CN", nameKey: "language.zhCN", nativeName: "简体中文", flag: "🇨🇳" },
  { code: "en-US", nameKey: "language.enUS", nativeName: "English", flag: "🇺🇸" },
] as const;

export type LanguageCode = (typeof languages)[number]["code"];
