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
        'src/**/*.d.ts',
        // Entry points and wiring: they compose tested units and cannot be
        // exercised without a live extension host. The logic they orchestrate
        // — health, proxy, badge, state, api — is covered directly, and the
        // wiring itself is covered by the Playwright extension suite.
        'src/popup/main.tsx',
        'src/popup/options.tsx',
        'src/background/index.ts',
        'src/manifest.config.ts',
        'src/test/**',
      ],
      // Gate G2: extension >= 80%
      thresholds: { lines: 80, functions: 80, branches: 80, statements: 80 },
    },
  },
});
