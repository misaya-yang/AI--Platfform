import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";

const AUTH_STORAGE_KEY = "agent-gateway-auth";
const LOGIN_USERNAME = process.env.E2E_USERNAME || "admin";
const LOGIN_PASSWORD = process.env.E2E_PASSWORD || "123456.dc";

async function buildAuthPayload(
  request: APIRequestContext,
  baseURL: string
): Promise<Record<string, unknown>> {
  const loginResponse = await request.post(`${baseURL}/api/v1/auth/login`, {
    data: {
      email: `${LOGIN_USERNAME}@hejazfs.com.au`,
      password: LOGIN_PASSWORD,
    },
  });

  if (!loginResponse.ok()) {
    const body = await loginResponse.text();
    throw new Error(`E2E login failed (${loginResponse.status()}): ${body}`);
  }

  const data = (await loginResponse.json()) as Record<string, unknown>;
  return {
    state: {
      token: data.access_token,
      user: data.user,
      isAuthenticated: true,
      forcePasswordChange: Boolean(data.force_password_change),
      rememberMe: true,
    },
    version: 0,
  };
}

async function seedAuth(page: Page, payload: Record<string, unknown>) {
  await page.addInitScript(
    ({ key, payload }) => {
      const serialized = JSON.stringify(payload);
      localStorage.setItem(key, serialized);
      sessionStorage.setItem(key, serialized);
    },
    { key: AUTH_STORAGE_KEY, payload }
  );
}

function normalizeRouteForFile(route: string): string {
  if (!route || route === "/") return "home";
  return route.replace(/^\//, "").replace(/[/?#:&=]+/g, "-");
}

test.beforeEach(async ({ page, request, baseURL }) => {
  const payload = await buildAuthPayload(request, baseURL || "http://127.0.0.1:5173");
  await seedAuth(page, payload);
});

test("walk main pages and detect obvious UI breakage", async ({ page, baseURL }, testInfo: TestInfo) => {
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

  page.on("console", (msg) => {
    if (msg.type() === "error") {
      const text = msg.text();
      if (!/favicon|NO_COLOR|forwardRef render functions accept exactly two parameters/i.test(text)) {
        consoleErrors.push(text);
      }
    }
  });
  page.on("pageerror", (err) => {
    pageErrors.push(String(err));
  });

  await page.goto("/dashboard");
  await expect(page).toHaveURL(/\/dashboard/);

  const navLinks = page.locator("aside a[href^='/']");
  const navCount = await navLinks.count();
  expect(navCount).toBeGreaterThan(0);

  const routes = new Set<string>(["/dashboard"]);
  for (let i = 0; i < navCount; i += 1) {
    const href = await navLinks.nth(i).getAttribute("href");
    if (href && href.startsWith("/")) routes.add(href);
  }

  const mainContent = page.locator(".ant-layout-content").first();

  for (const route of routes) {
    await page.goto(route);
    await expect(page).toHaveURL(new RegExp(`${route.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`));
    await expect(mainContent).toBeVisible();

    await expect(page.getByText(/something went wrong|application error|runtime error/i)).toHaveCount(0);

    const screenshotPath = testInfo.outputPath(`walkthrough-${normalizeRouteForFile(route)}.png`);
    await page.screenshot({ path: screenshotPath, fullPage: true });
  }

  const uniqueBadResponses = [...new Set(badResponses)];

  if (uniqueBadResponses.length > 0) {
    // Make failed-network diagnostics visible in CI/stdout and local runs.
    console.log("BAD_RESPONSES:\n" + uniqueBadResponses.join("\n"));
    await testInfo.attach("bad-responses.txt", {
      body: uniqueBadResponses.join("\n"),
      contentType: "text/plain",
    });
  }

  expect(pageErrors, `Page runtime errors:\n${pageErrors.join("\n")}`).toEqual([]);
  expect(consoleErrors, `Console errors:\n${consoleErrors.join("\n")}`).toEqual([]);
  expect(
    uniqueBadResponses,
    `Network responses >= 400:\n${uniqueBadResponses.join("\n")}`
  ).toEqual([]);

  if (baseURL) {
    await page.goto(baseURL);
  }
});
