import { defineConfig } from 'vitest/config';
import { resolve } from 'node:path';

export default defineConfig({
  resolve: {
    alias: { '@contracts': resolve(import.meta.dirname, '../contracts/generated') },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov', 'json-summary'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/main.tsx',
        'src/**/*.d.ts',
        // Monaco's glue: browser-only, and unloadable under jsdom (it needs
        // real workers, layout, and matchMedia). Same category as main.tsx —
        // it is exercised by the Playwright suite, not by unit tests. The
        // editor *contract* is covered: CodeEditor's fallback path and the
        // PlainEditor that implements the same props are both unit-tested.
        'src/components/editor/monaco/**',
      ],
      // Gate G2: web >= 80%
      thresholds: { lines: 80, functions: 80, branches: 80, statements: 80 },
    },
  },
});
