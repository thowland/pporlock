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
    //
    // Every HTML page NOT reachable from the manifest has to be listed here.
    // CRXJS walks the manifest, so the popup and devtools pages are found for
    // it — but `panel.html` is referenced only from JavaScript, in
    // `chrome.devtools.panels.create()`, and nothing can see that. Omitting it
    // built cleanly and shipped a DevTools panel pointing at a page that did
    // not exist, which Chrome renders as blank with no error anywhere (OI-28).
    rollupOptions: {
      input: {
        options: 'src/popup/options.html',
        panel: 'src/devtools/panel.html',
      },
    },
  },
  // CRXJS needs a stable port for its HMR client in dev; harmless in build.
  server: { host: '127.0.0.1', port: 5174, strictPort: true },
});
