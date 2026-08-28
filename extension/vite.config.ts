import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { crx } from '@crxjs/vite-plugin';
import { resolve } from 'node:path';
import manifest from './src/manifest.config';

export default defineConfig({
  plugins: [react(), crx({ manifest: manifest as never })],
  resolve: {
    alias: { '@contracts': resolve(import.meta.dirname, '../contracts/generated') },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    // The extension talks only to loopback; nothing here may reach a CDN.
    rollupOptions: { input: { options: 'src/popup/options.html' } },
  },
  // CRXJS needs a stable port for its HMR client in dev; harmless in build.
  server: { host: '127.0.0.1', port: 5174, strictPort: true },
});
