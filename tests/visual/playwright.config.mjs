import { defineConfig } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const visualDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(visualDir, '../..');

export default defineConfig({
  testDir: visualDir,
  testMatch: 'visual.spec.mjs',
  outputDir: path.join(repoRoot, 'test-results'),
  snapshotPathTemplate: '{testDir}/__screenshots__/{arg}{ext}',
  fullyParallel: false,
  workers: process.env.CI ? 1 : undefined,
  retries: 0,
  timeout: 30_000,
  updateSnapshots: process.env.UPDATE_BASELINES === 'true' ? 'all' : 'none',
  expect: {
    timeout: 10_000,
    toHaveScreenshot: {
      animations: 'disabled',
      maxDiffPixelRatio: 0.001
    }
  },
  reporter: [
    ['line'],
    ['json', { outputFile: path.join(repoRoot, 'test-results/results.json') }],
    ['html', { outputFolder: path.join(repoRoot, 'playwright-report'), open: 'never' }]
  ],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    browserName: 'chromium',
    colorScheme: 'light',
    locale: 'en-US',
    reducedMotion: 'reduce',
    serviceWorkers: 'block',
    timezoneId: 'UTC',
    trace: 'retain-on-failure'
  },
  webServer: {
    command: 'python3 -m http.server 4173 --bind 127.0.0.1',
    cwd: repoRoot,
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: false,
    timeout: 15_000
  }
});
