import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Proxy in dev so the browser never deals with CORS or a second origin.
    proxy: {
      '/v1': { target: process.env.JUICEBOX_API_URL ?? 'http://localhost:8000', changeOrigin: true },
      '/health': { target: process.env.JUICEBOX_API_URL ?? 'http://localhost:8000', changeOrigin: true },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
});
