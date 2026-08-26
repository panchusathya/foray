import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

function blockForayInternal() {
  // Mirrors packages/web/nginx.conf: /foray/internal spends money and is
  // reached over the Docker network by the API container, never the browser.
  return {
    name: 'block-foray-internal',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (req.url?.startsWith('/foray/internal')) {
          res.statusCode = 404;
          res.end();
          return;
        }
        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), blockForayInternal()],
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
