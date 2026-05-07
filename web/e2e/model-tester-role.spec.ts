/**
 * E2E tests for model_tester role — playground-only access.
 *
 * Validates that users with only `conversation:playground:access`
 * can log in, see only the Playground menu, and are blocked from
 * all other console pages.
 */
import { test, expect } from "@playwright/test";

const TEST_USERS = [
  { email: "islamic_tester_1@hejazfs.com.au", password: "Test1234.dc" },
  { email: "islamic_tester_2@hejazfs.com.au", password: "Test1234.dc" },
  { email: "islamic_tester_3@hejazfs.com.au", password: "Test1234.dc" },
  { email: "islamic_tester_4@hejazfs.com.au", password: "Test1234.dc" },
  { email: "islamic_tester_5@hejazfs.com.au", password: "Test1234.dc" },
];

async function login(page, email: string, password: string) {
  await page.goto("/login", { waitUntil: "domcontentloaded" });
  // The login form splits email into username + @hejazfs.com.au
  const username = email.replace("@hejazfs.com.au", "");
  await page.fill('input#email', username);
  await page.fill('input#password', password);
  await page.click('button[type="submit"]');
  // Wait for login API response and any redirect
  await page.waitForLoadState("networkidle");
}

for (const user of TEST_USERS) {
  test.describe(`model_tester user: ${user.email}`, () => {
    test("can log in and access playground", async ({ page }) => {
      await login(page, user.email, user.password);
      // Default redirect may be /dashboard which shows 403 for model_tester.
      // Navigate directly to playground to verify access.
      await page.goto("/assistant");
      await page.waitForLoadState("networkidle");
      // Verify playground content is visible
      const bodyText = await page.locator("body").textContent();
      expect(bodyText).toMatch(/New chat|Playground|模型体验|聊天|assistant|Sh Wahda/i);
    });

    test("sees only playground in sidebar", async ({ page }) => {
      await login(page, user.email, user.password);
      await page.goto("/assistant");
      await page.waitForLoadState("networkidle");

      // Wait for sidebar to render
      await page.waitForSelector("nav, [class*='sidebar'], aside", { timeout: 10000 });

      // Playground/模型体验 should be visible
      const playgroundVisible = await page.locator("text=/模型体验|Playground|New chat/i").first().isVisible().catch(() => false);
      expect(playgroundVisible).toBe(true);

      // Dashboard should NOT be visible in nav
      const dashboardVisible = await page.locator("nav >> text=/Dashboard|仪表盘/i").isVisible().catch(() => false);
      expect(dashboardVisible).toBe(false);

      // Services should NOT be visible
      const servicesVisible = await page.locator("nav >> text=/Services|服务/i").isVisible().catch(() => false);
      expect(servicesVisible).toBe(false);

      // Users should NOT be visible
      const usersVisible = await page.locator("nav >> text=/Users|用户/i").isVisible().catch(() => false);
      expect(usersVisible).toBe(false);
    });

    test("is blocked from dashboard (403)", async ({ page }) => {
      await login(page, user.email, user.password);
      // Try navigating to dashboard directly
      await page.goto("/dashboard");
      await page.waitForLoadState("networkidle");
      // Should show 403 or redirect away from dashboard
      const bodyText = await page.locator("body").textContent();
      const url = page.url();
      const is403 = bodyText?.includes("403") || bodyText?.includes("Access Denied") || bodyText?.includes("没有权限");
      const notDashboard = !url.includes("/dashboard");
      expect(is403 || notDashboard).toBe(true);
    });
  });
}
