import { expect, test, type Page, type TestInfo } from "@playwright/test";

const sidebarNavSelector = "aside a[href^='/'], [role='complementary'] a[href^='/']";

function normalizeRouteForFile(route: string): string {
  if (!route || route === "/") return "home";
  return route.replace(/^\//, "").replace(/[/?#:&=]+/g, "-");
}

async function collectVisibleRoutes(page: Page): Promise<string[]> {
  const navLinks = page.locator(sidebarNavSelector);
  const routes = new Set<string>(["/dashboard"]);
  await expect(navLinks.first()).toBeVisible({ timeout: 15_000 });
  const navCount = await navLinks.count();
  expect(navCount).toBeGreaterThan(0);

  for (let index = 0; index < navCount; index += 1) {
    const href = await navLinks.nth(index).getAttribute("href");
    if (href && href.startsWith("/")) {
      routes.add(href);
    }
  }

  return [...routes];
}

test("walk main pages and detect obvious UI breakage", async ({ page }, testInfo: TestInfo) => {
  test.setTimeout(5 * 60_000);

  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const badResponses: string[] = [];

  page.on("response", (response) => {
    const status = response.status();
    if (status < 400) return;

    const url = response.url();
    if (/favicon\.ico/i.test(url)) return;

    badResponses.push(`${status} ${url}`);
  });

  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const text = message.text();
    if (!/favicon|NO_COLOR/i.test(text)) {
      consoleErrors.push(text);
    }
  });

  page.on("pageerror", (error) => {
    pageErrors.push(String(error));
  });

  await page.goto("/dashboard", { waitUntil: "domcontentloaded" });
  await expect(page).toHaveURL(/\/dashboard/);

  const routes = await collectVisibleRoutes(page);
  const mainContent = page.locator(".ant-layout-content, main").first();

  for (const route of routes) {
    await page.goto(route, { waitUntil: "domcontentloaded" });
    await expect(page).toHaveURL(new RegExp(route.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
    await expect(page.locator(sidebarNavSelector).first()).toBeVisible({ timeout: 15_000 });
    await expect(mainContent).toBeVisible();
    await expect(page.getByText(/something went wrong|application error|runtime error/i)).toHaveCount(0);

    const screenshotPath = testInfo.outputPath(`walkthrough-${normalizeRouteForFile(route)}.png`);
    await page.screenshot({ path: screenshotPath });
  }

  const uniqueBadResponses = [...new Set(badResponses)];
  expect(pageErrors, `Page runtime errors:\n${pageErrors.join("\n")}`).toEqual([]);
  expect(consoleErrors, `Console errors:\n${consoleErrors.join("\n")}`).toEqual([]);
  expect(uniqueBadResponses, `Network responses >= 400:\n${uniqueBadResponses.join("\n")}`).toEqual([]);
});
