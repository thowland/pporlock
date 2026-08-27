import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';

// CRXJS is wired in at Sprint 5, when the MV3 manifest exists (SPEC-3 §2.1).
// Sprint 0 keeps the build plain so the toolchain is verifiable now.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@contracts': resolve(import.meta.dirname, '../contracts/generated') },
  },
  build: { outDir: 'dist', sourcemap: true },
});
