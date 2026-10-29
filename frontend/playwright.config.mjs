import { defineConfig } from '@playwright/test'

// Browser tests for the built frontend. They start no server: each test serves `dist/` and a
// fixture backend through Playwright request routing, and they use the Microsoft Edge that
// Windows already provides (no browser download). Run `npm run test:browser`.
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  workers: 1,
  reporter: [['list']],
  outputDir: './test-results',
  use: {
    channel: 'msedge',
    headless: true,
    baseURL: 'http://atlas.preview/',
    viewport: { width: 1440, height: 900 },
  },
})
