import { useEffect } from 'react';
import { ConfigProvider, App as AntApp, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import enUS from 'antd/locale/en_US';
import { useTranslation } from 'react-i18next';
import { AppRouter } from "@/router";
import { useAppStore } from "@/store/useAppStore";
import { lightTheme, darkTheme } from "@/theme/themeConfig";
import { Toaster } from "@/components/ui/toaster";

export default function App() {
  const { darkMode } = useAppStore();
  const { i18n } = useTranslation();
  const currentTheme = darkMode ? darkTheme : lightTheme;

  // Sync Tailwind dark mode class with app state
  useEffect(() => {
    if (darkMode) {
      document.documentElement.classList.add('dark');
    } else {
      document.documentElement.classList.remove('dark');
    }
  }, [darkMode]);

  // Select Ant Design locale based on current i18n language
  const antdLocale = i18n.language === 'en-US' ? enUS : zhCN;

  return (
    <ConfigProvider
      locale={antdLocale}
      theme={{
        ...currentTheme,
        algorithm: darkMode ? theme.darkAlgorithm : theme.defaultAlgorithm,
      }}
    >
      <AntApp>
        <AppRouter />
        <Toaster />
      </AntApp>
    </ConfigProvider>
  );
}
