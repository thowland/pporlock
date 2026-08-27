import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@contracts': resolve(import.meta.dirname, '../contracts/generated') },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
    // The daemon serves these assets from 127.0.0.1:8081 (REQ API-003).
    // No CDN, no external fonts, no runtime network beyond the daemon origin.
    assetsInlineLimit: 0,
  },
  server: { host: '127.0.0.1', port: 5173 },
});
