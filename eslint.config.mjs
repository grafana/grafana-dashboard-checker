// Lint settings for dashboard_checker/browser/scan.js. CI runs `npm run lint`; see CONTRIBUTING.md.
import js from '@eslint/js';

export default [
  js.configs.recommended,
  {
    files: ['dashboard_checker/browser/**/*.js'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        // k6 runtime
        __ENV: 'readonly',
        open: 'readonly',
        console: 'readonly',
        // The functions scan.js passes to page.evaluate() run in the page.
        document: 'readonly',
        setTimeout: 'readonly',
      },
    },
  },
];
