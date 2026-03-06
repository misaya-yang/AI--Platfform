import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import AxeBuilder from "@axe-core/playwright";
import { expect, type APIRequestContext, type Page } from "@playwright/test";

const helperDir = path.dirname(fileURLToPath(import.meta.url));
const ARTIFACT_DIR = path.resolve(helperDir, "../../.playwright");
const USER_FILE = path.join(ARTIFACT_DIR, "e2e-user.json");
const AUTH_STORAGE_KEY = "agent-gateway-auth";
const APP_STORAGE_KEY = "agent-gateway-storage";

export interface E2ETestUser {
  email: string;
  password: string;
}

export function getApiUrl(): string {
  const apiUrl = process.env.E2E_API_URL;
  if (!apiUrl) {
    throw new Error("Missing E2E_API_URL");
  }
  return apiUrl;
}

export function modKey(): "Meta" | "Control" {
  return process.platform === "darwin" ? "Meta" : "Control";
}

export async function readE2ETestUser(): Promise<E2ETestUser> {
  const raw = await fs.readFile(USER_FILE, "utf-8");
  return JSON.parse(raw) as E2ETestUser;
}

export async function ensureAuthenticatedPage(page: Page, destination: string) {
  const loginButtonName = /sign in|log in|login|登\s*录/i;

  await page.goto(destination);

  const loginFormVisible = await page
    .locator("#email")
    .isVisible()
    .catch(() => false);

  if (!loginFormVisible && !page.url().includes("/login")) {
    return;
  }

  const testUser = await readE2ETestUser();
  await page.locator("#email").fill(testUser.email);
  await page.locator("#password").fill(testUser.password);
  await page.getByRole("button", { name: loginButtonName }).click();
  await page.waitForURL(/\/(dashboard|assistant|playground)/, { timeout: 15000 });

  if (!page.url().includes(destination)) {
    await page.goto(destination);
  }
}

export async function loginThroughApi(request: APIRequestContext): Promise<{
  token: string;
  user: Record<string, unknown>;
}> {
  const testUser = await readE2ETestUser();
  const response = await request.post(`${getApiUrl()}/api/v1/auth/login`, {
    data: {
      email: testUser.email,
      password: testUser.password,
    },
  });

  if (!response.ok()) {
    throw new Error(`E2E login failed (${response.status()}): ${await response.text()}`);
  }

  const payload = (await response.json()) as Record<string, unknown>;
  return {
    token: String(payload.access_token || ""),
    user: (payload.user as Record<string, unknown>) || {},
  };
}

export async function buildAuthHeaders(request: APIRequestContext): Promise<Record<string, string>> {
  const { token } = await loginThroughApi(request);
  return {
    Authorization: `Bearer ${token}`,
  };
}

export async function seedClientPrefs(
  page: Page,
  prefs: {
    locale?: "zh-CN" | "en-US";
    themeMode?: "light" | "dark" | "system";
    resolvedTheme?: "light" | "dark";
    darkMode?: boolean;
  }
) {
  await page.addInitScript(
    ({ appStorageKey, locale, themeMode, resolvedTheme, darkMode }) => {
      if (locale) {
        localStorage.setItem("i18nextLng", locale);
      }
      if (themeMode && resolvedTheme) {
        localStorage.setItem(
          appStorageKey,
          JSON.stringify({
            state: {
              themeMode,
              resolvedTheme,
              darkMode:
                typeof darkMode === "boolean"
                  ? darkMode
                  : resolvedTheme === "dark",
            },
            version: 3,
          })
        );
      }
    },
    {
      appStorageKey: APP_STORAGE_KEY,
      locale: prefs.locale,
      themeMode: prefs.themeMode,
      resolvedTheme: prefs.resolvedTheme,
      darkMode: prefs.darkMode,
    }
  );
}

export async function clearAuthState(page: Page) {
  await page.addInitScript(({ authStorageKey, appStorageKey }) => {
    localStorage.removeItem(authStorageKey);
    sessionStorage.removeItem(authStorageKey);
    localStorage.removeItem(appStorageKey);
  }, { authStorageKey: AUTH_STORAGE_KEY, appStorageKey: APP_STORAGE_KEY });
}

export async function assertNoBlockingA11yIssues(page: Page, include: string[]) {
  const scanner = include.reduce(
    (builder, selector) => builder.include(selector),
    new AxeBuilder({ page })
  );
  const result = await scanner.analyze();
  const violations = result.violations.filter((violation) =>
    ["serious", "critical"].includes(violation.impact || "")
  );
  expect(violations).toEqual([]);
}

export async function assertInpBudget(page: Page, p75ThresholdMs = 200) {
  const p75 = await page.evaluate(
    () => window.__AI_GATEWAY_PERF__?.getInteractionP75?.() ?? 0
  );
  expect(p75).toBeLessThan(p75ThresholdMs);
}

export async function installTelemetryCollector(page: Page) {
  await page.evaluate(() => {
    const key = "__E2E_TELEMETRY_EVENTS__";
    (window as Window & { __E2E_TELEMETRY_EVENTS__?: unknown[] })[key] = [];
    window.addEventListener("ai-gateway:telemetry", (event) => {
      const detail = (event as CustomEvent).detail;
      (
        window as Window & { __E2E_TELEMETRY_EVENTS__?: unknown[] }
      ).__E2E_TELEMETRY_EVENTS__?.push(detail);
    });
  });
}

export async function readTelemetryEvents(page: Page): Promise<
  Array<{ event: string; payload?: Record<string, unknown> }>
> {
  return page.evaluate(() => {
    return (
      (
        window as Window & {
          __E2E_TELEMETRY_EVENTS__?: Array<{
            event: string;
            payload?: Record<string, unknown>;
          }>;
        }
      ).__E2E_TELEMETRY_EVENTS__ || []
    );
  });
}

export function toSseBody(events: Array<Record<string, unknown>>): string {
  return `${events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")}data: [DONE]\n\n`;
}
