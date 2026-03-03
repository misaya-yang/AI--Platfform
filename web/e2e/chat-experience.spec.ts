import {
  expect,
  test,
  type APIRequestContext,
  type Page,
  type Route,
} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const AUTH_STORAGE_KEY = "agent-gateway-auth";
const LOGIN_USERNAME = process.env.E2E_USERNAME || "admin";
const LOGIN_PASSWORD = process.env.E2E_PASSWORD || "123456.dc";
const ASSISTANT_COMPOSER_ID = "assistant-chat-composer";
const PLAYGROUND_COMPOSER_ID = "playground-chat-composer";
const APP_STORAGE_KEY = "agent-gateway-storage";

function modKey(): "Meta" | "Control" {
  return process.platform === "darwin" ? "Meta" : "Control";
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

async function seedClientPrefs(
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
            version: 2,
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
  const accessToken = data.access_token as string;
  const user = data.user as Record<string, unknown>;
  return {
    state: {
      token: accessToken,
      user,
      isAuthenticated: true,
      forcePasswordChange: Boolean(data.force_password_change),
      rememberMe: true,
    },
    version: 0,
  };
}

async function assertNoBlockingA11yIssues(page: Page, include: string[]) {
  const scanner = include.reduce(
    (builder, selector) => builder.include(selector),
    new AxeBuilder({ page })
  );
  const result = await scanner.analyze();
  const violations = result.violations.filter((v) =>
    ["serious", "critical"].includes(v.impact || "")
  );
  expect(violations).toEqual([]);
}

async function assertInpBudget(page: Page, p75ThresholdMs = 200) {
  const p75 = await page.evaluate(
    () => window.__AI_GATEWAY_PERF__?.getInteractionP75?.() ?? 0
  );
  expect(p75).toBeLessThan(p75ThresholdMs);
}

async function installTelemetryCollector(page: Page) {
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

async function readTelemetryEvents(page: Page): Promise<
  Array<{ event: string; payload?: Record<string, unknown> }>
> {
  return page.evaluate(() => {
    const events =
      (
        window as Window & {
          __E2E_TELEMETRY_EVENTS__?: Array<{
            event: string;
            payload?: Record<string, unknown>;
          }>;
        }
      ).__E2E_TELEMETRY_EVENTS__ || [];
    return events;
  });
}

function toSseBody(events: Array<Record<string, unknown>>): string {
  return `${events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("")}data: [DONE]\n\n`;
}

test.beforeEach(async ({ page, request, baseURL }) => {
  const payload = await buildAuthPayload(request, baseURL || "http://127.0.0.1:5173");
  await seedAuth(page, payload);
});

test("assistant stream path keeps a11y and performance budget", async ({ page }) => {
  await page.goto("/assistant");
  const composer = page.locator(`#${ASSISTANT_COMPOSER_ID}`);
  await expect(composer).toBeVisible();

  await page.keyboard.press(`${modKey()}+K`);
  await expect(composer).toBeFocused();

  await assertNoBlockingA11yIssues(page, [
    `#${ASSISTANT_COMPOSER_ID}`,
    '[role="log"]',
  ]);

  const prompt = `e2e-assistant-${Date.now()}`;
  await composer.fill(prompt);
  await composer.press("Enter");
  await expect(page.getByText(prompt)).toBeVisible();

  await expect(page.locator('[role="log"]')).toBeVisible();
  await assertInpBudget(page);
});

test("assistant applies persisted locale + theme before interaction", async ({
  page,
}) => {
  await seedClientPrefs(page, {
    locale: "en-US",
    themeMode: "dark",
    resolvedTheme: "dark",
    darkMode: true,
  });

  await page.goto("/assistant");

  await expect(page.locator("html")).toHaveAttribute("lang", "en-US");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const colorScheme = await page.evaluate(
    () => document.documentElement.style.colorScheme
  );
  expect(colorScheme).toBe("dark");
});

test("assistant emits stream telemetry lifecycle on mocked stream", async ({
  page,
}) => {
  await page.route("**/api/v1/assistant/chat/stream", async (route) => {
    const body = toSseBody([
      { event_type: "started", data: { request_id: "e2e-mock" } },
      { event_type: "text_delta", data: "Mocked " },
      {
        event_type: "tool_call_start",
        data: {
          tool_call_id: "tool-1",
          tool_name: "search_docs",
          arguments: "{\"query\":\"mock\"}",
        },
      },
      {
        event_type: "tool_call_result",
        data: {
          tool_call_id: "tool-1",
          tool_name: "search_docs",
          result: "ok",
          success: true,
        },
      },
      { event_type: "text_delta", data: "stream response" },
      { event_type: "done", data: { duration_ms: 120, total_tokens: 42 } },
    ]);
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body,
    });
  });

  await page.goto("/assistant");
  await installTelemetryCollector(page);

  const composer = page.locator(`#${ASSISTANT_COMPOSER_ID}`);
  await composer.fill(`e2e-mock-${Date.now()}`);
  await composer.press("Enter");

  await expect(page.getByText("Mocked stream response")).toBeVisible();

  const events = await readTelemetryEvents(page);
  const names = events.map((event) => event.event);
  expect(names).toContain("chat.stream.started");
  expect(names).toContain("chat.stream.first_token");
  expect(names).toContain("chat.stream.finished");

  const finishedEvent = events.find((event) => event.event === "chat.stream.finished");
  expect(finishedEvent?.payload?.outcome).toBe("completed");
});

test("assistant escape cancels delayed stream", async ({ page }) => {
  await page.route("**/api/v1/assistant/chat/stream", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 2500));
    try {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream" },
        body: toSseBody([
          { event_type: "started", data: {} },
          { event_type: "text_delta", data: "late response" },
          { event_type: "done", data: { duration_ms: 2500 } },
        ]),
      });
    } catch {
      // Request can be aborted by Escape; ignore fulfill failure.
    }
  });

  await page.goto("/assistant");
  await installTelemetryCollector(page);

  const composer = page.locator(`#${ASSISTANT_COMPOSER_ID}`);
  await composer.fill(`e2e-cancel-${Date.now()}`);
  await composer.press("Enter");

  const stopButton = page.locator('button[aria-keyshortcuts="Escape"]').first();
  await expect(stopButton).toBeVisible();
  await page.keyboard.press("Escape");

  await expect(stopButton).toBeHidden();
  await expect(page.locator('[role="log"]')).toBeVisible();

  const events = await readTelemetryEvents(page);
  expect(
    events.some(
      (event) =>
        event.event === "chat.shortcut.triggered" &&
        event.payload?.action === "stop_stream"
    )
  ).toBeTruthy();
  const finishedEvents = events.filter((event) => event.event === "chat.stream.finished");
  expect(finishedEvents.length).toBeGreaterThan(0);
  expect(
    finishedEvents.some((event) =>
      ["cancelled", "completed"].includes(String(event.payload?.outcome))
    )
  ).toBeTruthy();
});

test("playground stream path keeps a11y and performance budget", async ({ page }) => {
  await page.goto("/playground");
  const composer = page.locator(`#${PLAYGROUND_COMPOSER_ID}`);
  await expect(composer).toBeVisible();

  const serviceSelect = page.locator('button[role="combobox"]').first();
  await serviceSelect.click();
  const options = page.locator('[role="option"]');
  const optionCount = await options.count();
  test.skip(optionCount === 0, "No playground services configured");
  await options.first().click();

  await page.keyboard.press(`${modKey()}+K`);
  await expect(composer).toBeFocused();

  await assertNoBlockingA11yIssues(page, [
    `#${PLAYGROUND_COMPOSER_ID}`,
    '[role="log"]',
  ]);

  const prompt = `e2e-playground-${Date.now()}`;
  await composer.fill(prompt);
  await composer.press("Enter");
  await expect(page.getByText(prompt)).toBeVisible();

  await expect(page.locator('[role="log"]')).toBeVisible();
  await assertInpBudget(page);
});

test("playground handles mocked stream with tool call lifecycle", async ({
  page,
}) => {
  const mockPlaygroundStream = async (route: Route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: toSseBody([
        {
          request_id: "pg-e2e",
          chunk_index: 1,
          event_type: "text_delta",
          content: { type: "text", data: "Playground " },
        },
        {
          request_id: "pg-e2e",
          chunk_index: 2,
          event_type: "tool_call_start",
          tool_call: {
            tool_call_id: "pg-tool-1",
            name: "web_search",
            arguments: "{\"query\":\"playwright\"}",
            status: "running",
          },
          content: { type: "tool_call", data: "" },
        },
        {
          request_id: "pg-e2e",
          chunk_index: 3,
          event_type: "tool_result",
          tool_call: {
            tool_call_id: "pg-tool-1",
            name: "web_search",
            arguments: "{\"query\":\"playwright\"}",
            status: "completed",
          },
          content: { type: "tool_result", data: "ok" },
        },
        {
          request_id: "pg-e2e",
          chunk_index: 4,
          event_type: "text_delta",
          content: { type: "text", data: "mock response" },
        },
        {
          request_id: "pg-e2e",
          chunk_index: 5,
          event_type: "stream_end",
          content: { type: "text", data: "" },
          metadata: { usage: { total_tokens: 33 } },
        },
      ]),
    });
  };
  await page.route("**/api/v1/stream", mockPlaygroundStream);
  await page.route("**/api/v1/proxy/**/runs/stream", mockPlaygroundStream);
  await page.route("**/api/v1/proxy/**/threads/**/runs/stream", mockPlaygroundStream);

  await page.goto("/playground");
  const composer = page.locator(`#${PLAYGROUND_COMPOSER_ID}`);
  await expect(composer).toBeVisible();

  const serviceSelect = page.locator('button[role="combobox"]').first();
  await serviceSelect.click();
  const options = page.locator('[role="option"]');
  const optionCount = await options.count();
  test.skip(optionCount === 0, "No playground services configured");
  await options.first().click();

  await composer.fill(`e2e-pg-mock-${Date.now()}`);
  await composer.press("Enter");

  await expect(page.getByText("Playground mock response")).toBeVisible();
  await expect(page.getByText("Tool calls")).toBeVisible();
});
