import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

const BACKEND = process.env.GHOSTWRITER_BACKEND_ORIGIN ?? 'http://localhost:8004'

export default defineConfig({
  plugins: [react()],
  base: '/ghostwriter/',
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      // SPA emits /ghostwriter/api/... ; backend serves /api/... — strip here in dev
      // (in prod the app's own nginx strips it).
      '/ghostwriter/api': {
        target: BACKEND,
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/ghostwriter/, ''),
      },
    },
  },
  test: {
    environment: 'happy-dom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
})
