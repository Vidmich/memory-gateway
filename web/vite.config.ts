import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
// `vitest/config` rather than `vite`: it is the same `defineConfig` with the `test`
// block typed, which keeps the test setup in one file instead of two.
import { defineConfig } from 'vitest/config'

// The API is same-origin in production (app/api/spa.py serves the built assets), so the
// dev proxy exists to make development look the same: relative `/api` URLs everywhere,
// no CORS, and a refresh cookie that does not need SameSite=None.
const API_TARGET = process.env.VITE_API_PROXY ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: false },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
