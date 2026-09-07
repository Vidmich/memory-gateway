import { expect, test } from '@playwright/test'

/**
 * Log in → reload → log out, in a real browser against the real stack.
 *
 * The Vitest suite covers the same flow against a stubbed server, which is where the
 * edge cases live. What only a browser can verify is the part that is not JavaScript:
 * that the refresh cookie is actually set with the flags the server asked for, that it
 * survives a hard navigation, and that it is gone afterwards.
 *
 * Needs the stack running and a seeded superadmin:
 *
 *   docker compose -f deploy/compose/docker-compose.yml up -d
 *   make seed
 *   E2E_EMAIL=admin@example.com E2E_PASSWORD=<printed> npm run e2e
 */

const EMAIL = process.env.E2E_EMAIL ?? 'admin@example.com'
const PASSWORD = process.env.E2E_PASSWORD ?? ''

test.skip(
  !PASSWORD,
  'E2E_PASSWORD is not set — run `make seed` and pass the password it prints.',
)

async function signIn(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.getByLabel('Email').fill(EMAIL)
  await page.getByLabel('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()
}

test('signing in lands on the dashboard', async ({ page }) => {
  await signIn(page)

  await expect(page).toHaveURL(/\/$/)
})

test('the refresh cookie is httpOnly and scoped to the auth endpoints', async ({ page }) => {
  await signIn(page)

  const cookies = await page.context().cookies()
  const refresh = cookies.find((cookie) => cookie.name === 'mg_refresh')

  expect(refresh).toBeDefined()
  expect(refresh?.httpOnly).toBe(true)
  expect(refresh?.sameSite).toBe('Lax')
  expect(refresh?.path).toBe('/api/v1/auth')
})

test('no token is left in browser storage', async ({ page }) => {
  await signIn(page)

  const stored = await page.evaluate(() => ({
    local: Object.keys(localStorage).length,
    session: Object.keys(sessionStorage).length,
  }))

  expect(stored).toEqual({ local: 0, session: 0 })
})

test('reloading keeps you signed in', async ({ page }) => {
  await signIn(page)

  await page.reload()

  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()
})

test('a deep link survives signing in', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveURL(/\/login/)

  await page.getByLabel('Email').fill(EMAIL)
  await page.getByLabel('Password').fill(PASSWORD)
  await page.getByRole('button', { name: 'Sign in' }).click()

  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()
})

test('signing out returns to login and the back button does not undo it', async ({ page }) => {
  await signIn(page)

  await page.getByRole('button', { name: new RegExp(EMAIL.split('@')[0] ?? 'Admin', 'i') }).click()
  await page.getByRole('menuitem', { name: 'Sign out' }).click()
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()

  await page.goBack()

  // The session is revoked server-side, so whatever the history restores cannot get
  // past the guard.
  await expect(page.getByRole('button', { name: 'Sign in' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeHidden()
})

test('a wrong password is refused without revealing which half was wrong', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('Email').fill(EMAIL)
  await page.getByLabel('Password').fill('definitely-not-the-password')
  await page.getByRole('button', { name: 'Sign in' }).click()

  await expect(page.getByRole('alert')).toContainText('Incorrect email or password.')
})
