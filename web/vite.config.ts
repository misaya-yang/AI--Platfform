import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Load env file based on `mode` in the current working directory.
  const env = loadEnv(mode, process.cwd(), "");

  const apiTarget = env.VITE_API_URL || "http://localhost:8080";

  return {
    plugins: [react()],

    resolve: {
      alias: {
        "@": path.resolve(__dirname, "src"),
      },
    },

    server: {
      port: 3000,
      host: true, // Listen on all addresses
      proxy: {
        // API proxy - forwards all /api requests to backend
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          secure: false,
        },
        // WebSocket proxy for streaming
        "/ws": {
          target: apiTarget.replace("http", "ws"),
          ws: true,
          changeOrigin: true,
        },
      },
    },

    // Pre-bundle LangGraph SDK to avoid circular dependency issues
    optimizeDeps: {
      include: ["@langchain/langgraph-sdk", "@langchain/langgraph-sdk/react"],
    },

    build: {
      outDir: "dist",
      sourcemap: mode === "development",
      // Optimize chunk splitting
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.includes("@langchain")) return "langgraph";
            if (id.includes("react-dom") || id.includes("react-router")) return "vendor";
            if (id.includes("antd") || id.includes("@ant-design")) return "ui";
          },
        },
      },
    },

    // Define environment variables available in the app
    define: {
      __APP_VERSION__: JSON.stringify(process.env.npm_package_version || "2.0.0"),
    },
  };
});
