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

    build: {
      outDir: "dist",
      sourcemap: mode === "development",
      // Optimize chunk splitting
      rollupOptions: {
        output: {
          manualChunks(id) {
            // React core
            if (id.includes("node_modules/react/") || id.includes("node_modules/react-dom/") || id.includes("node_modules/react-router")) {
              return "vendor-react";
            }
            // Ant Design (largest dep)
            if (id.includes("node_modules/antd") || id.includes("node_modules/@ant-design")) {
              return "vendor-antd";
            }
            // Radix UI components
            if (id.includes("node_modules/@radix-ui")) {
              return "vendor-radix";
            }
            // Markdown / code highlight
            if (id.includes("node_modules/react-markdown") || id.includes("node_modules/remark") || id.includes("node_modules/rehype") || id.includes("node_modules/react-syntax-highlighter")) {
              return "vendor-markdown";
            }
            // Charts / visualization
            if (id.includes("node_modules/recharts") || id.includes("node_modules/d3")) {
              return "vendor-charts";
            }
            // i18n
            if (id.includes("node_modules/i18next") || id.includes("node_modules/react-i18next")) {
              return "vendor-i18n";
            }
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
