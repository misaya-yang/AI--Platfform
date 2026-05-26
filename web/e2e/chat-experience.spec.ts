import { expect, test, type Page, type Route } from "@playwright/test";
import {
  assertInpBudget,
  assertNoBlockingA11yIssues,
  ensureAuthenticatedPage,
  installTelemetryCollector,
  modKey,
  readTelemetryEvents,
  seedClientPrefs,
  toSseBody,
} from "./support/helpers";

const ASSISTANT_COMPOSER_ID = "assistant-chat-composer";
const PLAYGROUND_COMPOSER_ID = "playground-chat-composer";
const MOCK_ASSISTANT_SERVICE_ID = "__builtin_assistant__";
const MOCK_ASSISTANT_MODEL_ID = "gpt-4o";
const MOCK_PLAYGROUND_SERVICE_ID = "e2e-mock-playground";
const MOCK_PLAYGROUND_THREAD_ID = "e2e-mock-thread";
const MOCK_PLAYGROUND_TOOL_ID = "pg-tool-1";

type MockAssistantSession = {
  session_id: string;
  service_id: string;
  user_id: string;
  tenant_id: string;
  created_at: string;
  updated_at: string;
  metadata?: Record<string, unknown>;
  config?: Record<string, unknown>;
};

type MockPlaygroundSession = {
  session_id: string;
  service_id: string;
  created_at: string;
  updated_at: string;
  metadata?: Record<string, unknown>;
};

function jsonResponse(body: unknown) {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

function buildMockPlaygroundSession(
  sessionId: string,
  metadata?: Record<string, unknown>
): MockPlaygroundSession {
  const now = new Date().toISOString();
  return {
    session_id: sessionId,
    service_id: MOCK_PLAYGROUND_SERVICE_ID,
    created_at: now,
    updated_at: now,
    metadata,
  };
}

function buildMockAssistantSession(
  sessionId: string,
  metadata?: Record<string, unknown>,
  config?: Record<string, unknown>
): MockAssistantSession {
  const now = new Date().toISOString();
  return {
    session_id: sessionId,
    service_id: MOCK_ASSISTANT_SERVICE_ID,
    user_id: "e2e-user",
    tenant_id: "default",
    created_at: now,
    updated_at: now,
    metadata,
    config,
  };
}

async function installAssistantHarness(
  page: Page,
  fulfillStream: (route: Route) => Promise<void>
) {
  let sessionCounter = 0;
  const sessions = new Map<string, MockAssistantSession>();

  await page.route("**/api/v1/assistant/models", async (route) => {
    await route.fulfill(
      jsonResponse({
        models: [
          {
            id: MOCK_ASSISTANT_MODEL_ID,
            name: "GPT-4o",
            provider: "openai",
            context_window: 128000,
            max_output_tokens: 4096,
            supports_vision: true,
            supports_tools: true,
          },
        ],
      })
    );
  });

  await page.route("**/api/v1/assistant/datasets", async (route) => {
    await route.fulfill(jsonResponse({ datasets: [] }));
  });

  await page.route("**/api/v1/assistant/config", async (route) => {
    await route.fulfill(
      jsonResponse({
        default_model_id: MOCK_ASSISTANT_MODEL_ID,
        available_providers: ["openai"],
        kb_enabled: false,
        web_search_enabled: true,
      })
    );
  });

  await page.route("**/api/v1/assistant/sessions/*/artifacts", async (route) => {
    await route.fulfill(jsonResponse({ artifacts: [], total: 0 }));
  });

  const listAssistantSessions = async (route: Route) => {
    await route.fulfill(jsonResponse(Array.from(sessions.values())));
  };

  await page.route("**/api/v1/sessions?*", async (route) => {
    const requestUrl = new URL(route.request().url());
    if (requestUrl.searchParams.get("service_id") !== MOCK_ASSISTANT_SERVICE_ID) {
      await route.fallback();
      return;
    }
    await listAssistantSessions(route);
  });

  await page.route("**/api/v1/sessions", async (route) => {
    if (route.request().method() === "GET") {
      await listAssistantSessions(route);
      return;
    }

    const payload =
      (route.request().postDataJSON() as {
        metadata?: Record<string, unknown>;
        config?: Record<string, unknown>;
      } | null) || null;
    const sessionId = `e2e-assistant-session-${++sessionCounter}`;
    const session = buildMockAssistantSession(sessionId, payload?.metadata, payload?.config);
    sessions.set(sessionId, session);
    await route.fulfill(jsonResponse({ session_id: sessionId }));
  });

  await page.route("**/api/v1/sessions/*/history?*", async (route) => {
    await route.fulfill(jsonResponse([]));
  });

  await page.route("**/api/v1/sessions/*", async (route) => {
    const request = route.request();
    const sessionId = request.url().split("/").pop() || "";

    if (request.method() === "GET") {
      const session = sessions.get(sessionId);
      await route.fulfill(jsonResponse(session || {}));
      return;
    }

    const existing = sessions.get(sessionId);
    if (!existing) {
      await route.fulfill(jsonResponse({}));
      return;
    }

    const payload =
      (request.postDataJSON() as {
        metadata?: Record<string, unknown>;
        config?: Record<string, unknown>;
      } | null) || null;
    const nextSession: MockAssistantSession = {
      ...existing,
      updated_at: new Date().toISOString(),
      metadata: {
        ...(existing.metadata || {}),
        ...(payload?.metadata || {}),
      },
      config: {
        ...(existing.config || {}),
        ...(payload?.config || {}),
      },
    };
    sessions.set(sessionId, nextSession);
    await route.fulfill(jsonResponse(nextSession));
  });

  await page.route("**/api/v1/assistant/chat/stream", fulfillStream);
}

async function installPlaygroundHarness(page: Page) {
  let sessionCounter = 0;
  let streamHits = 0;
  const sessions = new Map<string, MockPlaygroundSession>();

  const listSessions = async (route: Route) => {
    const requestUrl = new URL(route.request().url());
    const requestedServiceId = requestUrl.searchParams.get("service_id");
    const items = Array.from(sessions.values()).filter((session) =>
      requestedServiceId ? session.service_id === requestedServiceId : true
    );
    await route.fulfill(jsonResponse(items));
  };

  await page.route("**/api/v1/proxy", async (route) => {
    await route.fulfill(
      jsonResponse({
        services: [
          {
            service_id: MOCK_PLAYGROUND_SERVICE_ID,
            service_name: "Playwright Mock Agent",
            enabled: true,
            service_type: "langgraph",
            metadata: {
              adapter_type: "langgraph",
              proxy_mode: "transparent",
            },
          },
        ],
      })
    );
  });

  await page.route("**/api/v1/sessions?*", listSessions);
  await page.route("**/api/v1/sessions", async (route) => {
    if (route.request().method() === "GET") {
      await listSessions(route);
      return;
    }

    const payload =
      (route.request().postDataJSON() as { metadata?: Record<string, unknown> } | null) || null;
    const sessionId = `e2e-playground-session-${++sessionCounter}`;
    const session = buildMockPlaygroundSession(sessionId, payload?.metadata);
    sessions.set(sessionId, session);
    await route.fulfill(jsonResponse(session));
  });

  await page.route("**/api/v1/sessions/*", async (route) => {
    const sessionId = route.request().url().split("/").pop() || "";
    const existing = sessions.get(sessionId);
    const payload =
      (route.request().postDataJSON() as { metadata?: Record<string, unknown> } | null) || null;

    if (!existing) {
      await route.fulfill(jsonResponse({}));
      return;
    }

    const nextSession: MockPlaygroundSession = {
      ...existing,
      updated_at: new Date().toISOString(),
      metadata: {
        ...(existing.metadata || {}),
        ...(payload?.metadata || {}),
      },
    };
    sessions.set(sessionId, nextSession);
    await route.fulfill(jsonResponse(nextSession));
  });

  await page.route("**/api/v1/sessions/*/messages", async (route) => {
    await route.fulfill(jsonResponse({ ok: true }));
  });

  await page.route(
    `**/api/v1/proxy/${MOCK_PLAYGROUND_SERVICE_ID}/threads`,
    async (route) => {
      await route.fulfill(jsonResponse({ thread_id: MOCK_PLAYGROUND_THREAD_ID }));
    }
  );

  await page.route(
    `**/api/v1/proxy/${MOCK_PLAYGROUND_SERVICE_ID}/runs/wait`,
    async (route) => {
      await route.fulfill(
        jsonResponse({
          messages: [
            {
              type: "tool",
              role: "tool",
              tool_call_id: MOCK_PLAYGROUND_TOOL_ID,
              name: "web_search",
              content: "ok",
            },
            {
              type: "AIMessage",
              role: "assistant",
              content: "Playground mock response",
              tool_calls: [
                {
                  id: MOCK_PLAYGROUND_TOOL_ID,
                  name: "web_search",
                  args: { query: "playwright" },
                },
              ],
            },
          ],
        })
      );
    }
  );

  await page.route(
    `**/api/v1/proxy/${MOCK_PLAYGROUND_SERVICE_ID}/threads/${MOCK_PLAYGROUND_THREAD_ID}/runs/wait`,
    async (route) => {
      await route.fulfill(
        jsonResponse({
          messages: [
            {
              type: "tool",
              role: "tool",
              tool_call_id: MOCK_PLAYGROUND_TOOL_ID,
              name: "web_search",
              content: "ok",
            },
            {
              type: "AIMessage",
              role: "assistant",
              content: "Playground mock response",
              tool_calls: [
                {
                  id: MOCK_PLAYGROUND_TOOL_ID,
                  name: "web_search",
                  args: { query: "playwright" },
                },
              ],
            },
          ],
        })
      );
    }
  );

  return {
    recordStreamHit() {
      streamHits += 1;
    },
    getStreamHits() {
      return streamHits;
    },
  };
}

async function selectFirstPlaygroundService(page: Page) {
  const serviceSelect = page.locator('button[role="combobox"]').first();
  await expect(serviceSelect).toBeVisible();
  await serviceSelect.click();
  const firstOption = page.locator('[role="option"]').first();
  const hasOptions = await firstOption
    .waitFor({ state: "visible", timeout: 5000 })
    .then(() => true)
    .catch(() => false);
  test.skip(!hasOptions, "No playground services configured");
  await firstOption.click();
}

test("assistant stream path keeps a11y and performance budget", async ({ page }) => {
  await ensureAuthenticatedPage(page, "/assistant");
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

test("assistant applies persisted locale + theme before interaction", async ({ page }) => {
  await seedClientPrefs(page, {
    locale: "en-US",
    themeMode: "dark",
    resolvedTheme: "dark",
    darkMode: true,
  });

  await ensureAuthenticatedPage(page, "/assistant");

  await expect(page.locator("html")).toHaveAttribute("lang", "en-US");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const colorScheme = await page.evaluate(() => document.documentElement.style.colorScheme);
  expect(colorScheme).toBe("dark");
});

test("assistant emits stream telemetry lifecycle on mocked stream", async ({ page }) => {
  await installAssistantHarness(page, async (route) => {
    const body = toSseBody([
      { event_type: "started", data: { request_id: "e2e-mock" } },
      { event_type: "text_delta", data: "Mocked " },
      {
        event_type: "tool_call_start",
        data: {
          tool_call_id: "tool-1",
          tool_name: "search_docs",
          arguments: '{"query":"mock"}',
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

  await ensureAuthenticatedPage(page, "/assistant");
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
  await installAssistantHarness(page, async (route) => {
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

  await ensureAuthenticatedPage(page, "/assistant");
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
  expect(
    events.some(
      (event) =>
        event.event === "chat.stream.finished" &&
        ["cancelled", "completed"].includes(String(event.payload?.outcome))
    )
  ).toBeTruthy();
});

test("playground stream path keeps a11y and performance budget", async ({ page }) => {
  await ensureAuthenticatedPage(page, "/playground");
  await selectFirstPlaygroundService(page);

  const composer = page.locator(`#${PLAYGROUND_COMPOSER_ID}`);
  await expect(composer).toBeVisible();

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

test("playground handles mocked stream with tool call lifecycle", async ({ page }) => {
  const harness = await installPlaygroundHarness(page);
  const mockPlaygroundStream = async (route: Route) => {
    harness.recordStreamHit();
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
            tool_call_id: MOCK_PLAYGROUND_TOOL_ID,
            name: "web_search",
            arguments: '{"query":"playwright"}',
            status: "running",
          },
          content: { type: "tool_call", data: "" },
        },
        {
          request_id: "pg-e2e",
          chunk_index: 3,
          event_type: "tool_result",
          tool_call: {
            tool_call_id: MOCK_PLAYGROUND_TOOL_ID,
            name: "web_search",
            arguments: '{"query":"playwright"}',
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

  await ensureAuthenticatedPage(page, "/playground");
  await selectFirstPlaygroundService(page);

  const composer = page.locator(`#${PLAYGROUND_COMPOSER_ID}`);
  await composer.fill(`e2e-pg-mock-${Date.now()}`);
  await composer.press("Enter");

  await expect(page.getByText("Playground mock response")).toBeVisible();
  await expect(page.getByText("web_search")).toBeVisible();
  expect(harness.getStreamHits()).toBeGreaterThan(0);
});

test("playground renders native langgraph tool events without heavy assistant card", async ({ page }) => {
  const harness = await installPlaygroundHarness(page);
  let requestedStreamMode: unknown;
  let waitHits = 0;
  await seedClientPrefs(page, {
    locale: "zh-CN",
    themeMode: "dark",
    resolvedTheme: "dark",
    darkMode: true,
  });

  const nativeLangGraphStream = async (route: Route) => {
    harness.recordStreamHit();
    requestedStreamMode = route.request().postDataJSON()?.stream_mode;
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: [
        'event: metadata\ndata: {"run_id":"pg-native-e2e"}\n\n',
        `event: messages\ndata: [{"content":[],"type":"AIMessageChunk","tool_call_chunks":[{"id":"${MOCK_PLAYGROUND_TOOL_ID}","name":"classify_query","index":0,"args":"{\\"query\\":"}],"usage_metadata":{"input_tokens":21,"output_tokens":4,"total_tokens":25}}]\n\n`,
        'event: messages\ndata: [{"content":[],"type":"AIMessageChunk","tool_call_chunks":[{"index":0,"args":"\\"imam\\"}"}]}]\n\n',
        `event: messages/complete\ndata: [{"content":"router: imam","type":"tool","role":"tool","tool_call_id":"${MOCK_PLAYGROUND_TOOL_ID}","name":"classify_query","status":"success"}]\n\n`,
        'event: messages\ndata: [{"content":"Imam ","type":"AIMessageChunk","role":"assistant"}]\n\n',
        'event: messages\ndata: [{"content":"native ","type":"AIMessageChunk","role":"assistant"}]\n\n',
        'event: messages\ndata: [{"content":"response","type":"AIMessageChunk","role":"assistant"}]\n\n',
        'event: messages/complete\ndata: [{"content":"Imam native response","type":"ai","role":"assistant"}]\n\n',
        "event: end\ndata: {}\n\n",
      ].join(""),
    });
  };
  const nativeWaitResponse = {
    messages: [
      {
        type: "tool",
        role: "tool",
        tool_call_id: MOCK_PLAYGROUND_TOOL_ID,
        name: "classify_query",
        content: "router: imam",
      },
      {
        type: "AIMessage",
        role: "assistant",
        content: "Imam native response",
        tool_calls: [
          {
            id: MOCK_PLAYGROUND_TOOL_ID,
            name: "classify_query",
            args: { query: "imam" },
          },
        ],
      },
    ],
  };

  await page.route("**/api/v1/proxy/**/runs/stream", nativeLangGraphStream);
  await page.route("**/api/v1/proxy/**/threads/**/runs/stream", nativeLangGraphStream);
  await page.route("**/api/v1/proxy/**/runs/wait", async (route) => {
    waitHits += 1;
    await route.fulfill(jsonResponse(nativeWaitResponse));
  });
  await page.route("**/api/v1/proxy/**/threads/**/runs/wait", async (route) => {
    waitHits += 1;
    await route.fulfill(jsonResponse(nativeWaitResponse));
  });

  await ensureAuthenticatedPage(page, "/playground");
  await selectFirstPlaygroundService(page);

  const composer = page.locator(`#${PLAYGROUND_COMPOSER_ID}`);
  await composer.fill(`e2e-pg-native-${Date.now()}`);
  await composer.press("Enter");

  await expect(page.getByText("Imam native response")).toBeVisible();
  await expect(page.getByText("classify_query")).toBeVisible();
  await expect(page.getByText("unknown_tool")).toHaveCount(0);
  await expect(page.locator('[data-message-supplemental="timeline"]')).toHaveCount(0);

  const assistantSurface = page
    .locator('[data-message-role="assistant"] [data-message-surface="assistant"]')
    .last();
  const surfaceStyles = await assistantSurface.evaluate((node) => {
    const styles = window.getComputedStyle(node);
    return {
      backgroundColor: styles.backgroundColor,
      boxShadow: styles.boxShadow,
    };
  });

  expect(surfaceStyles.backgroundColor).toBe("rgba(0, 0, 0, 0)");
  expect(surfaceStyles.boxShadow).toBe("none");
  expect(requestedStreamMode).toEqual(["messages-tuple", "updates", "custom"]);
  expect(waitHits).toBe(0);
  expect(harness.getStreamHits()).toBeGreaterThan(0);
});
