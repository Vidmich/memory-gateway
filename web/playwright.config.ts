import { defineConfig, devices } from '@playwright/test'

/**
 * End-to-end coverage of the one flow that cannot be faked: a real browser, the real
 * Vite build, and the real API talking to a real PostgreSQL.
 *
 * It needs the stack up, which is why it is a separate command rather than part of
 * `npm test`:
 *
 *   docker compose -f deploy/compose/docker-compose.yml up -d
 *   make seed                       # prints the superadmin password
 *   E2E_PASSWORD=... npm run e2e
 */
const BASE_URL = process.env.E2E_BASE_URL ?? 'http://localhost:5173'

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'list' : 'html',
  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
    // The dev stack is plain http, so a Secure cookie would be dropped; the app only
    // sets Secure in production (see app/api/control/auth.py).
    ignoreHTTPSErrors: true,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
})
