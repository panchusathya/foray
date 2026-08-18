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
      // Foray sourcing engine runs as a separate service; strip the /foray prefix.
      '/foray': {
        target: process.env.FORAY_API_URL ?? 'http://localhost:8100',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/foray/, ''),
      },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
});
