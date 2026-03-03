import { useEffect } from 'react';
import { ConfigProvider, App as AntApp, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import { useTranslation } from 'react-i18next';
import { AppRouter } from "@/router";
import { useAppStore } from "@/store/useAppStore";
import { useThemeSync } from "@/hooks/useThemeSync";
import { resolveAppLocale } from "@/i18n";
import { lightTheme, darkTheme } from "@/theme/themeConfig";
import { Toaster } from "@/components/ui/toaster";

export default function App() {
  const resolvedTheme = useThemeSync();
  const darkMode = useAppStore((s) => s.darkMode);
  const { i18n } = useTranslation();
  const currentTheme = darkMode ? darkTheme : lightTheme;

  // Keep document language in sync with app locale
  useEffect(() => {
    const locale = resolveAppLocale(i18n.resolvedLanguage || i18n.language);
    document.documentElement.lang = locale;
    document.documentElement.dir = "ltr";
  }, [i18n.language, i18n.resolvedLanguage]);

  // Select Ant Design locale based on current i18n language
  const locale = resolveAppLocale(i18n.resolvedLanguage || i18n.language);
  const antdLocale = locale === 'en-US' ? enUS : zhCN;

  return (
    <ConfigProvider
      locale={antdLocale}
      theme={{
        ...currentTheme,
        algorithm: resolvedTheme === "dark" ? theme.darkAlgorithm : theme.defaultAlgorithm,
      }}
    >
      <AntApp>
        <AppRouter />
        <Toaster />
      </AntApp>
    </ConfigProvider>
  );
}
