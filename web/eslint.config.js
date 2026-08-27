import js from '@eslint/js';
import tsParser from '@typescript-eslint/parser';
import tsPlugin from '@typescript-eslint/eslint-plugin';
import security from 'eslint-plugin-security';
import reactHooks from 'eslint-plugin-react-hooks';

export default [
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**'] },
  js.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      parser: tsParser,
      parserOptions: { ecmaVersion: 2022, sourceType: 'module', ecmaFeatures: { jsx: true } },
      globals: { chrome: 'readonly', window: 'readonly', document: 'readonly',
                 console: 'readonly', fetch: 'readonly', setTimeout: 'readonly',
                 clearTimeout: 'readonly', setInterval: 'readonly',
                 clearInterval: 'readonly', EventSource: 'readonly',
                 localStorage: 'readonly', requestAnimationFrame: 'readonly' },
    },
    plugins: { '@typescript-eslint': tsPlugin, security, 'react-hooks': reactHooks },
    rules: {
      ...tsPlugin.configs.recommended.rules,
      ...security.configs.recommended.rules,
      // Catches stale-closure and identity-churn bugs in hooks — the class of
      // bug that makes a subscription effect re-run on every render.
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'no-undef': 'off',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_' }],
      '@typescript-eslint/no-explicit-any': 'error',
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      eqeqeq: ['error', 'always'],
    },
  },
  {
    files: ['**/*.test.{ts,tsx}'],
    languageOptions: {
      globals: { describe: 'readonly', it: 'readonly', expect: 'readonly',
                 vi: 'readonly', beforeEach: 'readonly', afterEach: 'readonly' },
    },
    rules: { 'security/detect-object-injection': 'off' },
  },
];
