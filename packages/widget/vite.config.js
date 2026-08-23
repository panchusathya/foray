import { defineConfig } from 'vite';

export default defineConfig({
  build: {
    lib: {
      entry: 'src/index.js',
      name: 'JuiceboxIntakeWidget',
      formats: ['iife'],
      fileName: () => 'juicebox-intake-widget.js',
    },
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: true,
    rollupOptions: {
      output: {
        exports: 'named',
      },
    },
  },
});
