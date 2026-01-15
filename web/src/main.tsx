// Emergency reset: Clear localStorage BEFORE React loads if ?reset=true is in URL
// This prevents infinite loops and frozen pages from corrupted state
(function emergencyReset() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("reset") === "true") {
    console.log("[Emergency Reset] Clearing localStorage to recover from stuck state");
    localStorage.removeItem("agent-gateway-storage");
    // Remove the reset param from URL without reload
    const newUrl = window.location.pathname;
    window.history.replaceState({}, "", newUrl);
  }
})();

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import "./index.css";
import "./i18n"; // 初始化i18n国际化
import App from "./App";

const queryClient = new QueryClient();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>
);
