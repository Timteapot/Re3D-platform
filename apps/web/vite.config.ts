/// <reference types="vitest/config" />

import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // CPU checks run without local Vite environment files. Normal development and
  // release builds keep the existing environment and proxy configuration.
  const isPlatformCheck = process.env.RE3D_PLATFORM_CHECK === "1";
  const environment: Record<string, string> = isPlatformCheck
    ? {}
    : loadEnv(mode, process.cwd(), "");
  const apiTarget = environment.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000";

  if (!/^https?:\/\/[^/]+(?::\d+)?$/.test(apiTarget)) {
    throw new Error("VITE_API_PROXY_TARGET must be an HTTP(S) origin without a path");
  }

  return {
    envDir: isPlatformCheck ? false : undefined,
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
        },
      },
    },
    preview: {
      port: 4173,
      strictPort: true,
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test/setup.ts",
      restoreMocks: true,
    },
  };
});
