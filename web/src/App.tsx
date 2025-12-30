import { ConfigProvider, App as AntApp, theme } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { AppRouter } from "@/router";
import { useAppStore } from "@/store/useAppStore";
import { lightTheme, darkTheme } from "@/theme/themeConfig";

export default function App() {
  const { darkMode } = useAppStore();
  const currentTheme = darkMode ? darkTheme : lightTheme;

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        ...currentTheme,
        algorithm: darkMode ? theme.darkAlgorithm : theme.defaultAlgorithm,
      }}
    >
      <AntApp>
        <AppRouter />
      </AntApp>
    </ConfigProvider>
  );
}
