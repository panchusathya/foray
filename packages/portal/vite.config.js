import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// The candidate portal. Port 5174 so it runs alongside the recruiter app in
// dev, and the proxy covers only /v1/portal — the same narrowing the
// production nginx enforces, so a route that works in dev cannot be one the
// deployed portal is unable to reach.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      '/v1/portal': {
        target: process.env.JUICEBOX_API_URL ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
});
