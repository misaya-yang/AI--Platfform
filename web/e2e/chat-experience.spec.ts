import { expect, test, type Page, type Route } from "@playwright/test";
import {
  assertInpBudget,
  assertNoBlockingA11yIssues,
  ensureAuthenticatedPage,
  installClientAuth,
  installTelemetryCollector,
  modKey,
  readTelemetryEvents,
  seedClientPrefs,
  toSseBody,
} from "./support/helpers";
import { buildTimeline } from "../src/pages/assistant/components/buildTimeline";
import type { ChatMessage as AssistantChatMessage } from "../src/pages/assistant/types";

const ASSISTANT_COMPOSER_ID = "assistant-chat-composer";
const PLAYGROUND_COMPOSER_ID = "playground-chat-composer";
const MOCK_ASSISTANT_SERVICE_ID = "__builtin_assistant__";
const MOCK_ASSISTANT_MODEL_ID = "gpt-4o";
const MOCK_PLAYGROUND_SERVICE_ID = "e2e-mock-playground";
const MOCK_PLAYGROUND_THREAD_ID = "e2e-mock-thread";
const MOCK_PLAYGROUND_TOOL_ID = "pg-tool-1";

const translateDefault = (
  key: string,
  options?: Record<string, unknown>
): string => String(options?.defaultValue ?? key);

test("assistant timeline tolerates transient tool entries without names", () => {
  const message = {
    id: "assistant-transient-tool-name",
    role: "assistant",
    content: "",
    createdAt: new Date(0).toISOString(),
    toolCalls: [
      {
        id: "todo-call-transient",
        name: undefined,
        arguments: { todos: [{ content: "Review tool stream", status: "pending" }] },
        status: "running",
      },
    ],
    processSummary: {
      collapsed: true,
      status: "running",
      steps: [],
      tools: [
        {
          id: "process-tool-transient",
          name: undefined,
          status: "running",
        },
      ],
    },
  } as unknown as AssistantChatMessage;

  const timeline = buildTimeline(message, translateDefault);

  expect(timeline.steps).toHaveLength(2);
  expect(timeline.steps.map((step) => step.title)).toEqual([
    "External tool",
    "External tool",
  ]);
});

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

type MockAssistantModel = {
  id: string;
  name: string;
  provider: string;
  context_window: number;
  max_output_tokens: number;
  supports_vision: boolean;
  supports_tools: boolean;
};

type MockAssistantMessage = {
  role: "user" | "assistant";
  content: string;
  timestamp?: string;
  metadata?: Record<string, unknown>;
};

type MockAssistantArtifact = {
  artifact_id: string;
  type: string;
  format: string;
  title: string;
  filename: string;
  mime_type?: string;
  size_bytes?: number;
  source?: string;
  download_url?: string;
  created_at?: string;
};

function jsonResponse(body: unknown) {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

function pathSegmentAfter(url: string, segment: string): string {
  const parts = new URL(url).pathname.split("/");
  const index = parts.lastIndexOf(segment);
  return index >= 0 ? parts[index + 1] || "" : "";
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
  fulfillStream: (route: Route) => Promise<void>,
  options: {
    models?: MockAssistantModel[];
    defaultModelId?: string;
    availableProviders?: string[];
    preloadedSessions?: MockAssistantSession[];
    historyBySessionId?: Record<string, MockAssistantMessage[]>;
    artifactsBySessionId?: Record<string, MockAssistantArtifact[]>;
    onCreateShare?: (
      sessionId: string,
      payload: { include_artifacts?: boolean }
    ) => void;
  } = {}
) {
  let sessionCounter = 0;
  const sessions = new Map<string, MockAssistantSession>(
    (options.preloadedSessions || []).map((session) => [session.session_id, session])
  );
  const historyBySessionId = options.historyBySessionId || {};
  const artifactsBySessionId = options.artifactsBySessionId || {};
  const models = options.models ?? [
    {
      id: MOCK_ASSISTANT_MODEL_ID,
      name: "GPT-4o",
      provider: "openai",
      context_window: 128000,
      max_output_tokens: 4096,
      supports_vision: true,
      supports_tools: true,
    },
  ];
  const defaultModelId = options.defaultModelId ?? models[0]?.id ?? "";
  const availableProviders = options.availableProviders ?? [
    ...new Set(models.map((model) => model.provider)),
  ];

  await page.route("**/api/v1/assistant/models", async (route) => {
    await route.fulfill(
      jsonResponse({
        models,
      })
    );
  });

  await page.route("**/api/v1/assistant/datasets", async (route) => {
    await route.fulfill(jsonResponse({ datasets: [] }));
  });

  await page.route("**/api/v1/assistant/config", async (route) => {
    await route.fulfill(
      jsonResponse({
        default_model_id: defaultModelId,
        available_providers: availableProviders,
        kb_enabled: false,
        web_search_enabled: true,
      })
    );
  });

  await page.route("**/api/v1/assistant/local-nodes*", async (route) => {
    await route.fulfill(jsonResponse({ devices: [] }));
  });

  await page.route("**/api/v1/assistant/sessions/*/artifacts", async (route) => {
    const sessionId = pathSegmentAfter(route.request().url(), "sessions");
    const artifacts = artifactsBySessionId[sessionId] || [];
    await route.fulfill(jsonResponse({ artifacts, total: artifacts.length }));
  });

  await page.route("**/api/v1/assistant/sessions/*/share", async (route) => {
    const sessionId = pathSegmentAfter(route.request().url(), "sessions");
    const payload =
      (route.request().postDataJSON() as { include_artifacts?: boolean } | null) || {};
    options.onCreateShare?.(sessionId, payload);
    const artifacts = payload.include_artifacts ? artifactsBySessionId[sessionId] || [] : [];
    await route.fulfill(
      jsonResponse({
        share_code: "e2e-share",
        share_url: "/share/e2e-share",
        title: sessions.get(sessionId)?.metadata?.title || null,
        message_count: historyBySessionId[sessionId]?.length || 0,
        artifact_count: artifacts.length,
        created_at: new Date().toISOString(),
        expires_at: null,
      })
    );
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
    const sessionId = pathSegmentAfter(route.request().url(), "sessions");
    await route.fulfill(jsonResponse(historyBySessionId[sessionId] || []));
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
  await expect(composer).toBeEnabled();

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

test("assistant route admits permitted users", async ({ page }) => {
  await installClientAuth(page, {
    user_id: "e2e-assistant-route-user",
    email: "assistant-route@example.com",
    display_name: "Assistant Route",
    roles: ["user"],
    permissions: ["console:dashboard:view", "conversation:playground:access"],
    effective_permissions: ["console:dashboard:view", "conversation:playground:access"],
  });
  await installAssistantHarness(page, async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: toSseBody([{ event_type: "done", data: { duration_ms: 0 } }]),
    });
  });

  await page.goto("/assistant");
  await expect(page).toHaveURL(/\/assistant/);
  await expect(page.locator(`#${ASSISTANT_COMPOSER_ID}`)).toBeVisible();
  await expect(page.locator('a[href="/assistant"]')).toBeVisible();
});

test("assistant shows the active Confluence connector count", async ({ page }) => {
  await seedClientPrefs(page, { locale: "en-US" });
  await installClientAuth(page, {
    user_id: "e2e-connector-count-user",
    email: "connector-count@example.com",
    display_name: "Connector Count",
    roles: ["user"],
    permissions: ["console:dashboard:view", "conversation:playground:access"],
    effective_permissions: ["console:dashboard:view", "conversation:playground:access"],
  });
  await installAssistantHarness(page, async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: toSseBody([{ event_type: "done", data: { duration_ms: 0 } }]),
    });
  });
  await page.route("**/api/v1/connectors/available", async (route) => {
    await route.fulfill(jsonResponse([
      { provider: "confluence", display_name: "Confluence", description: "Atlassian wiki", enabled: true, connected: true },
      { provider: "github", display_name: "GitHub", description: "Code and issues", enabled: true, connected: true },
    ]));
  });

  await page.goto("/assistant");
  await page.locator("button:has(svg.lucide-plus)").last().click();
  await expect(page.getByRole("button", { name: /connectors|连接器/i })).toContainText("2");
});

test("assistant route keeps model testers playground-only", async ({ page }) => {
  await installClientAuth(page, {
    user_id: "e2e-model-tester-route-user",
    email: "model-tester-route@example.com",
    display_name: "Model Tester Route",
    roles: ["model_tester"],
    permissions: ["conversation:playground:access"],
    effective_permissions: ["conversation:playground:access"],
  });
  await installPlaygroundHarness(page);

  await page.goto("/assistant");
  await expect(page).toHaveURL(/\/playground/);
  await expect(page.locator('a[href="/assistant"]')).toHaveCount(0);
  await expect(page.locator(`#${PLAYGROUND_COMPOSER_ID}`)).toBeVisible();
});

test("assistant disables sending when no models are available", async ({ page }) => {
  await seedClientPrefs(page, { locale: "en-US" });
  await installClientAuth(page, {
    user_id: "e2e-assistant-no-model-user",
    email: "assistant-no-model@example.com",
    display_name: "Assistant No Model",
    roles: ["user"],
    permissions: ["console:dashboard:view", "conversation:playground:access"],
    effective_permissions: ["console:dashboard:view", "conversation:playground:access"],
  });
  let streamHits = 0;
  await installAssistantHarness(
    page,
    async (route) => {
      streamHits += 1;
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream" },
        body: toSseBody([{ event_type: "done", data: { duration_ms: 0 } }]),
      });
    },
    { models: [], defaultModelId: "", availableProviders: [] }
  );

  await page.goto("/assistant");
  await expect(page).toHaveURL(/\/assistant/);

  const composer = page.locator(`#${ASSISTANT_COMPOSER_ID}`);
  await expect(composer).toBeVisible();
  await expect(composer).toBeDisabled();
  await expect(composer).toHaveAttribute("placeholder", "No models available");

  const sendButton = page.locator('button[aria-keyshortcuts*="Enter"]').last();
  await expect(sendButton).toBeDisabled();
  await page.keyboard.press("Enter");
  expect(streamHits).toBe(0);
});

test("assistant ignores empty and duplicate same-tick Enter submissions", async ({ page }) => {
  await seedClientPrefs(page, { locale: "en-US" });
  await installClientAuth(page, {
    user_id: "e2e-assistant-send-guard-user",
    email: "assistant-send-guard@example.com",
    display_name: "Assistant Send Guard",
  });
  let streamHits = 0;
  await installAssistantHarness(page, async (route) => {
    streamHits += 1;
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: toSseBody([
        { event_type: "text_delta", data: "Guarded response" },
        { event_type: "done", data: { duration_ms: 10 } },
      ]),
    });
  });

  await page.goto("/assistant");
  const composer = page.locator(`#${ASSISTANT_COMPOSER_ID}`);
  await expect(composer).toBeEnabled();

  await composer.press("Enter");
  expect(streamHits).toBe(0);

  await composer.fill("send once");
  await composer.evaluate((element) => {
    element.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    element.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  });

  await expect.poll(() => streamHits).toBe(1);
  await expect(page.getByText("send once", { exact: true })).toHaveCount(1);
});

test("assistant mobile history uses a bounded overlay sheet", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await seedClientPrefs(page, { locale: "en-US" });
  await installClientAuth(page, {
    user_id: "e2e-assistant-mobile-history-user",
    email: "assistant-mobile-history@example.com",
    display_name: "Assistant Mobile History",
  });
  const session = buildMockAssistantSession("mobile-history-session", {
    title: "Mobile history conversation",
  });
  await installAssistantHarness(
    page,
    async (route) => {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream" },
        body: toSseBody([{ event_type: "done", data: { duration_ms: 0 } }]),
      });
    },
    { preloadedSessions: [session] },
  );

  await page.goto("/assistant");
  const historyToggle = page.getByRole("button", { name: /show history/i });
  await expect(historyToggle).toBeVisible();
  await historyToggle.click();

  const historySheet = page.getByRole("dialog", { name: /history/i });
  await expect(historySheet).toBeVisible();
  const bounds = await historySheet.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.width).toBeLessThanOrEqual(390);
  expect(bounds!.width).toBeGreaterThan(300);

  await page.keyboard.press("Escape");
  await expect(historySheet).toBeHidden();
});

test("assistant deletes a session through the runtime cleanup route", async ({ page }) => {
  test.setTimeout(30_000);
  await seedClientPrefs(page, { locale: "en-US" });
  await installClientAuth(page, {
    user_id: "e2e-assistant-delete-user",
    email: "assistant-delete@example.com",
    display_name: "Assistant Delete",
  });
  const title = "Delete through assistant runtime";
  const session = buildMockAssistantSession("delete-runtime-session", { title });
  await installAssistantHarness(
    page,
    async (route) => {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream" },
        body: toSseBody([{ event_type: "done", data: { duration_ms: 0 } }]),
      });
    },
    { preloadedSessions: [session] }
  );
  let deleteHits = 0;
  await page.route(
    "**/api/v1/assistant/sessions/delete-runtime-session",
    async (route) => {
      deleteHits += 1;
      await route.fulfill(jsonResponse({ status: "deleted" }));
    }
  );

  await page.goto("/assistant");
  await page.getByLabel("Show history").click();
  await page.getByRole("button", { name: `Delete: ${title}` }).click();

  await expect(page.getByRole("button", { name: `Delete: ${title}` })).toHaveCount(0);
  expect(deleteHits).toBe(1);
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

test("assistant renders the complete sub-agent status contract", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await seedClientPrefs(page, {
    locale: "en-US",
    themeMode: "dark",
    resolvedTheme: "dark",
    darkMode: true,
  });
  await installClientAuth(page, {
    user_id: "e2e-subagent-status-user",
    email: "subagent-status@example.com",
    display_name: "Sub-agent Status",
  });
  await installAssistantHarness(page, async (route) => {
    const started = (agentId: string, description: string) => ({
      event_type: "subagent_started",
      data: {
        agent_id: agentId,
        agent_type: "task",
        description,
        prompt: `Handle ${description}`,
        profile_id: `profile-${agentId}`,
        profile_name: `${description} agent`,
        delegation_id: "status-contract-batch",
      },
    });
    const finished = (
      agentId: string,
      status: string,
      options: {
        error?: string;
        limitations?: string[];
        resultSummary?: string;
        evidenceSummary?: string;
      } = {},
    ) => ({
      event_type: "subagent_finished",
      data: {
        agent_id: agentId,
        status,
        result_summary: options.resultSummary,
        error: options.error,
        duration_ms: 42,
        turns: 1,
        tool_calls: 1,
        result: {
          status,
          limitations: options.limitations || [],
          evidence: options.evidenceSummary
            ? [{ evidence_id: `evidence-${agentId}`, tool_name: "search_docs", summary: options.evidenceSummary }]
            : [],
          usage: { model_turns: 1, tool_calls: 1 },
        },
      },
    });

    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: toSseBody([
        { event_type: "started", data: { request_id: "subagent-status-contract" } },
        started("sub-running", "Running research"),
        {
          event_type: "subagent_step",
          data: { agent_id: "sub-running", step: "Turn 1/3", status: "running" },
        },
        started("sub-completed", "Completed research"),
        {
          event_type: "subagent_step",
          data: { agent_id: "sub-completed", step: "Verify sources", status: "running" },
        },
        {
          event_type: "subagent_tool_start",
          data: { agent_id: "sub-completed", tool_name: "search_docs", call_id: "search-1" },
        },
        {
          event_type: "subagent_tool_result",
          data: {
            agent_id: "sub-completed",
            call_id: "search-1",
            success: true,
            summary: "Two sources verified",
          },
        },
        finished("sub-completed", "completed", {
          resultSummary: "Completed with verified evidence.",
          evidenceSummary: "Two sources verified",
        }),
        started("sub-failed", "Failed research"),
        finished("sub-failed", "failed", { error: "Provider unavailable" }),
        started("sub-cancelled", "Cancelled research"),
        finished("sub-cancelled", "cancelled", { error: "Cancelled by parent" }),
        started("sub-blocked", "Blocked research"),
        finished("sub-blocked", "blocked", {
          error: "Approval required",
          limitations: ["Blocked on operator approval"],
        }),
        started("sub-partial", "Partial research"),
        finished("sub-partial", "partial", {
          resultSummary: "Available evidence is incomplete.",
          limitations: ["Partial data only"],
        }),
        started("sub-unknown", "Unknown terminal research"),
        finished("sub-unknown", "future_terminal"),
        { event_type: "text_delta", data: "Sub-agent status contract rendered." },
        { event_type: "done", data: { duration_ms: 80 } },
      ]),
    });
  });

  await page.goto("/assistant");
  const composer = page.locator(`#${ASSISTANT_COMPOSER_ID}`);
  await composer.fill(`subagent-status-${Date.now()}`);
  await composer.press("Enter");
  const agentsLauncher = page.getByRole("button", { name: /^Agents 7$/ });
  await agentsLauncher.click();
  await expect(page.getByRole("dialog", { name: "Sub-agent workbench" })).toBeVisible();
  await expect(page.getByTestId("subagent-workbench")).toBeVisible();
  await expect(page.getByRole("button", { name: "Close sub-agent workbench" })).toBeFocused();
  await expect(page.getByTestId("subagent-workbench")).toContainText("1 active · 6 completed");
  await page.waitForTimeout(400); // Let the drawer opacity transition reach its final contrast.
  await assertNoBlockingA11yIssues(page, ['[data-testid="subagent-workbench"]']);
  if (process.env.SUBAGENT_MOBILE_SCREENSHOT) {
    await page.screenshot({ path: process.env.SUBAGENT_MOBILE_SCREENSHOT, fullPage: false });
  }

  const statusCases = [
    ["sub-running", "running", "Running"],
    ["sub-completed", "completed", "Completed"],
    ["sub-failed", "failed", "Failed"],
    ["sub-cancelled", "cancelled", "Cancelled"],
    ["sub-blocked", "blocked", "Blocked"],
    ["sub-partial", "partial", "Partial"],
  ] as const;
  for (const [agentId, status, label] of statusCases) {
    const card = page.locator(`[data-subagent-id="${agentId}"]`);
    await expect(card).toHaveAttribute("data-status", status);
    await expect(card.getByLabel(`Status: ${label}`, { exact: true })).toBeVisible();
  }

  await expect(page.locator('[data-subagent-id="sub-running"]')).toContainText("Turn 1/3");
  await page.locator('[data-subagent-id="sub-blocked"] button').first().click();
  await expect(page.locator('[data-subagent-id="sub-blocked"]')).toContainText(
    "Blocked on operator approval",
  );
  await page.locator('[data-subagent-id="sub-partial"] button').first().click();
  await expect(page.locator('[data-subagent-id="sub-partial"]')).toContainText(
    "Partial data only",
  );
  await page.locator('[data-subagent-id="sub-completed"] button').first().click();
  await expect(page.locator('[data-subagent-id="sub-completed"]')).toContainText(
    "Two sources verified",
  );

  const unknownCard = page.locator('[data-subagent-id="sub-unknown"]');
  await expect(unknownCard).toHaveAttribute("data-status", "failed");
  await unknownCard.locator("button").first().click();
  await expect(unknownCard).toContainText(
    "Unsupported sub-agent status: future_terminal",
  );

  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Sub-agent workbench" })).toBeHidden();
  await expect(agentsLauncher).toBeFocused();

});

test("desktop sub-agent workbench follows three parallel children through out-of-order fan-in", async ({ page }) => {
  await seedClientPrefs(page, {
    locale: "en-US",
    themeMode: "dark",
    resolvedTheme: "dark",
    darkMode: true,
  });
  await installClientAuth(page, {
    user_id: "e2e-subagent-mobile-user",
    email: "subagent-mobile@example.com",
    display_name: "Sub-agent Parallel QA",
  });
  await installAssistantHarness(page, async (route) => {
    await route.fulfill({ status: 500, body: "The browser stream fixture was not installed" });
  });

  const frames: Array<{ delayMs: number; event: Record<string, unknown> }> = [
    { delayMs: 20, event: { event_type: "started", data: { request_id: "parallel-mobile" } } },
    ...[0, 1, 2].map((index) => ({
      delayMs: 60 + index * 20,
      event: {
        event_type: "subagent_started",
        data: {
          agent_id: `parallel-${index}`,
          agent_type: "task",
          description: [
            "Analyze controlling employment law authorities",
            "Inspect SEC filing risk disclosures",
            "Cross-check cited evidence and limitations",
          ][index],
          prompt: "private assignment containing sk-not-for-display-123456789",
          profile_id: ["legal-primary", "finance-filings", "evidence-reviewer"][index],
          profile_name: ["Legal primary-source analyst", "SEC filing analyst", "Evidence reviewer"][index],
          source_plugin: "community-doublecheck",
          delegation_id: "parallel-realistic-1",
          dispatch_index: index,
          attempt_id: "attempt-mobile-1",
        },
      },
    })),
    {
      delayMs: 130,
      event: { event_type: "subagent_step", data: { agent_id: "parallel-0", step: "Checking controlling cases", status: "running" } },
    },
    {
      delayMs: 150,
      event: { event_type: "subagent_tool_start", data: { agent_id: "parallel-1", call_id: "filing-1", tool_name: "filing_lookup", arguments: { api_key: "secret-raw-args" } } },
    },
    // Reconnect duplicates must not create extra children or tool rows.
    {
      delayMs: 170,
      event: { event_type: "subagent_started", data: { agent_id: "parallel-1", agent_type: "task", description: "Inspect SEC filing risk disclosures", delegation_id: "parallel-realistic-1", dispatch_index: 1 } },
    },
    {
      delayMs: 190,
      event: { event_type: "subagent_tool_start", data: { agent_id: "parallel-1", call_id: "filing-1", tool_name: "filing_lookup" } },
    },
    {
      delayMs: 210,
      event: { event_type: "subagent_text_delta", data: { agent_id: "parallel-0", text: "private chain-of-thought sk-never-render-123456789" } },
    },
    {
      delayMs: 1_500,
      event: {
        event_type: "subagent_finished",
        data: {
          agent_id: "parallel-2",
          status: "completed",
          result_summary: "Evidence cross-check completed.",
          duration_ms: 1_320,
          turns: 3,
          tool_calls: 2,
          result: {
            evidence: [{ evidence_id: "court-opinion-1", tool_name: "court_opinion_lookup", status: "completed", summary: "Controlling opinion verified" }],
            limitations: [],
            usage: { model_turns: 3, tool_calls: 2 },
            structured_payload: {
              verdict: "verified",
              confidence: "high",
              api_key: "structured-secret-raw",
            },
          },
          effective_execution: { model_id: "qwen3.7-plus", extensions: 1 },
        },
      },
    },
    // A replayed conflicting terminal and late step cannot rewrite/resurrect it.
    { delayMs: 1_540, event: { event_type: "subagent_finished", data: { agent_id: "parallel-2", status: "failed", error: "conflicting replay" } } },
    { delayMs: 1_560, event: { event_type: "subagent_step", data: { agent_id: "parallel-2", step: "Late replayed turn", status: "running" } } },
    { delayMs: 1_580, event: { event_type: "subagent_future_event", data: { agent_id: "parallel-2", instruction: "ignore unknown event" } } },
    {
      delayMs: 2_700,
      event: {
        event_type: "subagent_finished",
        data: {
          agent_id: "parallel-0",
          status: "failed",
          error: "Primary authority service unavailable",
          duration_ms: 2_520,
          result: { limitations: ["Could not retrieve the controlling opinion"] },
        },
      },
    },
    {
      delayMs: 3_900,
      event: {
        event_type: "subagent_finished",
        data: {
          agent_id: "parallel-1",
          status: "cancelled",
          error: "Cancelled by parent stream",
          duration_ms: 3_700,
          result: { limitations: ["Parent run stopped before synthesis"] },
        },
      },
    },
    { delayMs: 4_100, event: { event_type: "text_delta", data: "Parallel research concluded with mixed outcomes." } },
    { delayMs: 4_180, event: { event_type: "done", data: { duration_ms: 4_180 } } },
  ];

  await page.addInitScript(({ streamFrames }) => {
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string"
        ? input
        : input instanceof Request
          ? input.url
          : String(input);
      if (!url.includes("/api/v1/assistant/chat/stream")) return nativeFetch(input, init);
      const encoder = new TextEncoder();
      return new Response(new ReadableStream({
        start(controller) {
          let closed = false;
          const close = () => {
            if (closed) return;
            closed = true;
            controller.enqueue(encoder.encode("data: [DONE]\n\n"));
            controller.close();
          };
          for (const frame of streamFrames) {
            window.setTimeout(() => {
              if (!closed) controller.enqueue(encoder.encode(`data: ${JSON.stringify(frame.event)}\n\n`));
            }, frame.delayMs);
          }
          window.setTimeout(close, Math.max(...streamFrames.map((frame) => frame.delayMs)) + 80);
          init?.signal?.addEventListener("abort", close, { once: true });
        },
      }), { headers: { "content-type": "text/event-stream" } });
    };
  }, { streamFrames: frames });

  await page.goto("/assistant");
  const composer = page.locator(`#${ASSISTANT_COMPOSER_ID}`);
  await composer.fill("Run a parallel legal, finance, and evidence review");
  await composer.press("Enter");

  const launcher = page.getByRole("button", { name: "Open 3 sub-agents" });
  await expect(launcher).toContainText("3 active");
  await launcher.click();
  const dialog = page.getByTestId("subagent-workbench");
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("3 active · 0 completed");
  await expect(dialog.locator('[data-subagent-id="parallel-1"]')).toContainText("Using filing lookup");
  await expect(dialog).not.toContainText("private chain-of-thought");
  await expect(dialog).not.toContainText("sk-never-render");
  await expect(dialog).not.toContainText("secret-raw-args");

  await expect(dialog).toContainText("2 active · 1 completed", { timeout: 2_200 });
  await expect(dialog.locator('[data-subagent-id="parallel-2"]')).toHaveAttribute("data-status", "completed");
  await expect(dialog.locator('[data-subagent-id="parallel-2"]')).not.toContainText("conflicting replay");
  await expect(dialog.locator('[data-subagent-id="parallel-2"]')).not.toContainText("Late replayed turn");
  await page.waitForTimeout(400);
  if (process.env.SUBAGENT_DESKTOP_SCREENSHOT) {
    await page.screenshot({ path: process.env.SUBAGENT_DESKTOP_SCREENSHOT, fullPage: false });
  }

  await dialog.locator('[data-subagent-id="parallel-2"] button').first().click();
  await expect(dialog.locator('[data-subagent-id="parallel-2"]')).toContainText("Controlling opinion verified");
  await expect(dialog.locator('[data-subagent-id="parallel-2"]')).toContainText("Structured result");
  await dialog.locator('[data-subagent-id="parallel-2"] details summary').click();
  await expect(dialog).not.toContainText("structured-secret-raw");
  await expect(dialog).toContainText("1 active · 2 completed", { timeout: 3_200 });
  await expect(dialog).toContainText("0 active · 3 completed", { timeout: 4_500 });
  await expect(dialog.locator('[data-subagent-id="parallel-0"]')).toHaveAttribute("data-status", "failed");
  await expect(dialog.locator('[data-subagent-id="parallel-1"]')).toHaveAttribute("data-status", "cancelled");
  await expect(dialog).toContainText("All child terminals received; parent synthesis can continue.");
  await expect(dialog).toContainText("Child steering and per-agent cancellation are not available");
  await page.waitForTimeout(400);
  await assertNoBlockingA11yIssues(page, ['[data-testid="subagent-workbench"]']);
});

test("assistant restores todo_write after transient nameless tool events", async ({ page }) => {
  await seedClientPrefs(page, { locale: "en-US" });
  await installClientAuth(page, {
    user_id: "e2e-assistant-tool-order-user",
    email: "assistant-tool-order@example.com",
    display_name: "Assistant Tool Order",
  });
  const pageErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));

  await installAssistantHarness(page, async (route) => {
    const common = {
      run_id: "qwen-todo-run",
      thread_id: "qwen-todo-thread",
      session_id: "qwen-todo-session",
    };
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: toSseBody([
        { event_type: "started", data: { request_id: "qwen-todo-request" } },
        {
          event_type: "tool_call_end",
          data: {
            ...common,
            tool_call_id: "qwen-todo-call",
            timestamp: 1000,
          },
        },
        {
          event_type: "tool_call_result",
          data: {
            ...common,
            tool_call_id: "qwen-todo-call",
            result: { updated: 1 },
            success: true,
            timestamp: 1001,
          },
        },
        {
          event_type: "tool_call_start",
          data: {
            ...common,
            tool_call_id: "qwen-todo-call",
            name: "todo_write",
            tool_name: "todo_write",
            arguments: {
              todos: [{ content: "Review tool stream", status: "in_progress" }],
            },
            step_id: "qwen-todo-step",
            timestamp: 1002,
          },
        },
        {
          event_type: "tool_call_result",
          data: {
            ...common,
            tool_call_id: "qwen-todo-call",
            name: "todo_write",
            tool_name: "todo_write",
            result: { updated: 1 },
            success: true,
            timestamp: 1003,
          },
        },
        { event_type: "text_delta", data: "Todo list updated." },
        { event_type: "done", data: { duration_ms: 120, total_tokens: 42 } },
      ]),
    });
  });

  await ensureAuthenticatedPage(page, "/assistant");
  const composer = page.locator(`#${ASSISTANT_COMPOSER_ID}`);
  await composer.fill(`qwen-todo-${Date.now()}`);
  await composer.press("Enter");

  await expect(page.getByText("Todo list updated.")).toBeVisible();
  await expect(page.getByText("Failed to render message")).toHaveCount(0);
  await page.getByRole("button", { name: /Activity/ }).last().click();
  await expect(page.getByText("todo_write")).toBeVisible();
  expect(pageErrors).not.toContainEqual(
    expect.stringContaining("Cannot read properties of undefined")
  );
});

test("assistant treats protocol-completed todo_read result without success as completed", async ({ page }) => {
  await seedClientPrefs(page, { locale: "en-US" });
  await installClientAuth(page, {
    user_id: "e2e-assistant-tool-status-user",
    email: "assistant-tool-status@example.com",
    display_name: "Assistant Tool Status",
  });

  await installAssistantHarness(page, async (route) => {
    const common = {
      run_id: "qwen-todo-read-run",
      thread_id: "qwen-todo-read-thread",
      session_id: "qwen-todo-read-session",
    };
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: toSseBody([
        { event_type: "run_started", data: { ...common, timestamp: 1000 } },
        {
          event_type: "step_started",
          data: {
            ...common,
            step_id: "qwen-todo-read-step",
            title: "Execute tool: todo_read",
            timestamp: 1001,
          },
        },
        {
          event_type: "tool_call_start",
          data: {
            ...common,
            tool_call_id: "qwen-todo-read-call",
            name: "todo_read",
            tool_name: "todo_read",
            arguments: {},
            step_id: "qwen-todo-read-step",
            timestamp: 1002,
          },
        },
        {
          event_type: "tool_call_result",
          data: {
            ...common,
            tool_call_id: "qwen-todo-read-call",
            name: "todo_read",
            tool_name: "todo_read",
            status: "completed",
            result_preview: '{"todos":[]}',
            duration_ms: 8,
            timestamp: 1003,
          },
        },
        {
          event_type: "tool_call_end",
          data: {
            ...common,
            tool_call_id: "qwen-todo-read-call",
            name: "todo_read",
            status: "completed",
            duration_ms: 8,
            timestamp: 1004,
          },
        },
        {
          event_type: "step_finished",
          data: {
            ...common,
            step_id: "qwen-todo-read-step",
            status: "completed",
            duration_ms: 9,
            timestamp: 1005,
          },
        },
        { event_type: "text_delta", data: "Todo list is empty." },
        {
          event_type: "run_finished",
          data: { ...common, status: "succeeded", timestamp: 1006 },
        },
        { event_type: "done", data: { duration_ms: 10, total_tokens: 32 } },
      ]),
    });
  });

  await ensureAuthenticatedPage(page, "/assistant");
  const composer = page.locator(`#${ASSISTANT_COMPOSER_ID}`);
  await composer.fill(`qwen-todo-read-${Date.now()}`);
  await composer.press("Enter");

  await expect(page.getByText("Todo list is empty.")).toBeVisible();
  await page.getByRole("button", { name: /Activity/ }).last().click();
  await expect(page.getByText("Execute tool: todo_read")).toBeVisible();
  await expect(page.getByText("Step failed", { exact: true })).toHaveCount(0);
});

test("assistant keeps explicit todo_read failure as an error", async ({ page }) => {
  await seedClientPrefs(page, { locale: "en-US" });
  await installClientAuth(page, {
    user_id: "e2e-assistant-tool-error-user",
    email: "assistant-tool-error@example.com",
    display_name: "Assistant Tool Error",
  });

  await installAssistantHarness(page, async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: toSseBody([
        {
          event_type: "tool_call_start",
          data: {
            tool_call_id: "qwen-todo-read-error-call",
            name: "todo_read",
            tool_name: "todo_read",
            arguments: {},
            timestamp: 1000,
          },
        },
        {
          event_type: "tool_call_result",
          data: {
            tool_call_id: "qwen-todo-read-error-call",
            name: "todo_read",
            tool_name: "todo_read",
            status: "error",
            success: false,
            error: "todo_read denied",
            timestamp: 1001,
          },
        },
        {
          event_type: "tool_call_end",
          data: {
            tool_call_id: "qwen-todo-read-error-call",
            name: "todo_read",
            status: "error",
            error: "todo_read denied",
            timestamp: 1002,
          },
        },
        { event_type: "text_delta", data: "Tool request failed safely." },
        { event_type: "done", data: { duration_ms: 10, total_tokens: 20 } },
      ]),
    });
  });

  await ensureAuthenticatedPage(page, "/assistant");
  const composer = page.locator(`#${ASSISTANT_COMPOSER_ID}`);
  await composer.fill(`qwen-todo-read-error-${Date.now()}`);
  await composer.press("Enter");

  await expect(page.getByText("Tool request failed safely.")).toBeVisible();
  await page.getByRole("button", { name: /Activity/ }).last().click();
  await expect(page.getByText("todo_read denied", { exact: true })).toBeVisible();
});

test("assistant activity surfaces agent run state approvals context and artifacts", async ({ page }) => {
  await seedClientPrefs(page, { locale: "en-US" });
  await installClientAuth(page, {
    user_id: "e2e-assistant-agent-state-user",
    email: "assistant-agent-state@example.com",
    display_name: "Assistant Agent State",
    roles: ["user"],
    permissions: ["console:dashboard:view", "conversation:playground:access"],
    effective_permissions: ["console:dashboard:view", "conversation:playground:access"],
  });
  await installAssistantHarness(page, async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: toSseBody([
        { event_type: "run_started", data: { run_id: "f010-run", timestamp: 1000 } },
        {
          event_type: "task_planning",
          data: {
            goal: "Prepare deployment review",
            tasks: [
              {
                id: "plan",
                description: "Review requested change",
                type: "analysis",
                dependencies: [],
              },
              {
                id: "execute",
                description: "Draft final answer",
                type: "writing",
                dependencies: ["plan"],
              },
            ],
            parallel_groups: [],
          },
        },
        {
          event_type: "working_memory_update",
          data: {
            goal: "Prepare deployment review",
            tasks: [
              {
                id: "plan",
                description: "Review requested change",
                status: "completed",
                result: "Checklist ready",
              },
              {
                id: "execute",
                description: "Draft final answer",
                status: "in_progress",
              },
            ],
            collected_info: [
              { key: "context", value: "policy", source: "mock" },
            ],
            notes: ["Using remembered operator preference"],
            progress: { total: 2, completed: 1, failed: 0, percentage: 50 },
          },
        },
        {
          event_type: "tool_call_start",
          data: {
            tool_call_id: "approval-tool",
            tool_name: "shell",
            arguments: { cmd: "pnpm -C web build" },
            timestamp: 1100,
          },
        },
        {
          event_type: "approval_required",
          data: {
            tool_id: "approval-tool",
            tool_name: "shell",
            approval_id: "approval-1",
            reason: "Needs operator approval before running command",
          },
        },
        {
          event_type: "context_budget",
          data: {
            used_tokens: 2048,
            model_context_window: 128000,
            dropped_history_messages: 1,
          },
        },
        {
          event_type: "context_compacted",
          data: { compacted: true, dropped_history_messages: 1 },
        },
        {
          event_type: "artifact_created",
          data: {
            artifact_id: "artifact-f010",
            type: "document",
            format: "md",
            title: "Run report",
            filename: "run-report.md",
            mime_type: "text/markdown",
            size_bytes: 2048,
            download_url: "/mock-artifacts/run-report.md",
          },
        },
        { event_type: "text_delta", data: "Run state ready." },
        { event_type: "done", data: { duration_ms: 120, total_tokens: 42 } },
      ]),
    });
  });

  await page.goto("/assistant");

  const composer = page.locator(`#${ASSISTANT_COMPOSER_ID}`);
  await expect(composer).toBeVisible();
  await composer.fill(`e2e-agent-state-${Date.now()}`);
  await composer.press("Enter");

  await expect(page.getByText("Run state ready.")).toBeVisible();
  await expect(page.getByText("Run report").first()).toBeVisible();

  await page.getByRole("button", { name: /Activity/ }).last().click();
  await expect(page.getByText("Review requested change")).toBeVisible();
  await expect(page.getByText("Draft final answer")).toBeVisible();
  await expect(page.getByText("Needs operator approval before running command")).toBeVisible();
  await expect(page.getByText("Context used")).toBeVisible();
  await expect(page.getByText("Context compacted")).toBeVisible();
});

test("assistant restores session artifacts into unique artifact and share counts", async ({ page }) => {
  await seedClientPrefs(page, { locale: "en-US" });
  await installClientAuth(page, {
    user_id: "e2e-assistant-session-artifacts-user",
    email: "assistant-session-artifacts@example.com",
    display_name: "Assistant Session Artifacts",
    roles: ["user"],
    permissions: ["console:dashboard:view", "conversation:playground:access"],
    effective_permissions: ["console:dashboard:view", "conversation:playground:access"],
  });

  const sessionId = "e2e-restored-artifacts-session";
  const artifactId = "artifact-f011";
  const now = new Date().toISOString();
  const title = `restored-artifacts-${Date.now()}`;
  const shareRequests: Array<{ sessionId: string; includeArtifacts?: boolean }> = [];

  await installAssistantHarness(
    page,
    async (route) => {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream" },
        body: toSseBody([{ event_type: "done", data: { duration_ms: 0 } }]),
      });
    },
    {
      preloadedSessions: [
        {
          ...buildMockAssistantSession(sessionId, { title }),
          created_at: now,
          updated_at: now,
        },
      ],
      historyBySessionId: {
        [sessionId]: [
          {
            role: "user",
            content: "Recover my generated plan",
            timestamp: now,
          },
          {
            role: "assistant",
            content: "Here is the recovered plan.",
            timestamp: now,
            metadata: { artifact_ids: [artifactId] },
          },
        ],
      },
      artifactsBySessionId: {
        [sessionId]: [
          {
            artifact_id: artifactId,
            type: "document",
            format: "md",
            title: "Recovered plan",
            filename: "recovered-plan.md",
            mime_type: "text/markdown",
            size_bytes: 1024,
            source: "ai",
            download_url: "/mock-artifacts/recovered-plan.md",
            created_at: now,
          },
        ],
      },
      onCreateShare: (createdSessionId, payload) => {
        shareRequests.push({
          sessionId: createdSessionId,
          includeArtifacts: payload.include_artifacts,
        });
      },
    }
  );

  await page.goto("/assistant");
  await page.getByRole("button", { name: /show history/i }).first().click();
  await page.getByRole("button", { name: title, exact: true }).click();

  await expect(page.getByText("Recover my generated plan")).toBeVisible();
  await expect(page.getByText("Here is the recovered plan.")).toBeVisible();
  await expect(page.getByText("Recovered plan").first()).toBeVisible();

  const artifactsChip = page.getByRole("button", { name: /^Artifacts\s+1$/ });
  await expect(artifactsChip).toBeVisible();
  await artifactsChip.click();
  await expect(page.getByText("recovered-plan.md").first()).toBeVisible();

  await page.getByRole("button", { name: /^Share$/ }).click();
  const shareDialog = page.locator(".fixed.inset-0").filter({ hasText: "Share Conversation" }).first();
  await expect(shareDialog.getByText("Share Conversation")).toBeVisible();
  await expect(shareDialog.getByText("Artifacts")).toBeVisible();
  await expect(shareDialog.getByText("(1)")).toBeVisible();

  await shareDialog.getByRole("button", { name: "Create Share Link" }).click();
  await expect(shareDialog.getByText("Share link is ready!")).toBeVisible();
  expect(shareRequests).toEqual([{ sessionId, includeArtifacts: true }]);
});

test("assistant restores a pending approval after refresh", async ({ page }) => {
  await seedClientPrefs(page, { locale: "en-US" });
  await installClientAuth(page, {
    user_id: "e2e-assistant-reconnect-user",
    email: "assistant-reconnect@example.com",
    display_name: "Assistant Reconnect",
  });

  const sessionId = "e2e-reconnect-session";
  const runId = "e2e-reconnect-run";
  const approvalId = "e2e-reconnect-approval";
  const title = `reconnect-approval-${Date.now()}`;
  const now = new Date().toISOString();
  const consoleErrors: string[] = [];
  const requestFailures: string[] = [];
  let runStatusHits = 0;
  let runSucceeded = false;

  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  page.on("requestfailed", (request) => {
    requestFailures.push(`${request.method()} ${new URL(request.url()).pathname}`);
  });

  await installAssistantHarness(
    page,
    async (route) => {
      await route.fulfill({
        status: 200,
        headers: { "content-type": "text/event-stream" },
        body: toSseBody([{ event_type: "done", data: { duration_ms: 0 } }]),
      });
    },
    {
      preloadedSessions: [
        buildMockAssistantSession(sessionId, {
          title,
          assistant_active_run: {
            run_id: runId,
            updated_at: now,
          },
        }),
      ],
      historyBySessionId: {
        [sessionId]: [
          { role: "user", content: "Run the guarded command", timestamp: now },
          {
            role: "assistant",
            content: "Awaiting operator approval.",
            timestamp: now,
            metadata: {
              tool_calls: [
                { id: "guarded-shell", name: "shell", arguments: {}, status: "completed" },
              ],
            },
          },
        ],
      },
    }
  );
  await page.route(`**/api/v1/assistant/runs/${runId}`, async (route) => {
    runStatusHits += 1;
    await route.fulfill(
      jsonResponse({
        run: {
          run_id: runId,
          session_id: sessionId,
          status: runSucceeded ? "succeeded" : "running",
          checkpoint: runSucceeded
            ? { phase: "completed" }
            : {
                phase: "approval_pending",
                approval_id: approvalId,
                pending_tool: { tool_id: "guarded-shell", tool_name: "shell" },
              },
        },
      })
    );
  });
  await page.goto("/assistant");
  await page.getByRole("button", { name: /show history/i }).first().click();
  await page.getByRole("button", { name: title, exact: true }).click();
  await expect(page.getByText("Awaiting operator approval.")).toBeVisible();

  const hitsBeforeRefresh = runStatusHits;
  await page.reload();
  await expect.poll(() => runStatusHits).toBeGreaterThan(hitsBeforeRefresh);
  await page.getByRole("button", { name: /Activity/ }).last().click();
  await expect(page.getByText("Approval required: shell")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Reject" })).toBeVisible();

  runSucceeded = true;
  const hitsBeforeTerminalRefresh = runStatusHits;
  await page.reload();
  await expect.poll(() => runStatusHits).toBeGreaterThan(hitsBeforeTerminalRefresh);
  await page.getByRole("button", { name: /Activity/ }).last().click();
  await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Reject" })).toHaveCount(0);
  await expect(page.getByText(/^completed ·/)).toBeVisible();
  expect(consoleErrors).toEqual([]);
  expect(requestFailures).toEqual([]);
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

test("playground mobile history uses a bounded overlay sheet", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await seedClientPrefs(page, { locale: "en-US" });
  await installClientAuth(page, {
    user_id: "e2e-playground-mobile-history-user",
    email: "playground-mobile-history@example.com",
    display_name: "Playground Mobile History",
  });
  await installPlaygroundHarness(page);

  await page.goto("/playground");
  const historyToggle = page.getByRole("button", { name: /show history/i });
  await expect(historyToggle).toBeVisible();
  await historyToggle.click();

  const historySheet = page.getByRole("dialog", { name: /history/i });
  await expect(historySheet).toBeVisible();
  const bounds = await historySheet.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.width).toBeLessThanOrEqual(390);
  expect(bounds!.width).toBeGreaterThan(300);

  await page.keyboard.press("Escape");
  await expect(historySheet).toBeHidden();
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
        'event: messages\ndata: [{"content":[],"type":"AIMessageChunk","tool_call_chunks":[{"index":0,"args":"\\"agent\\""}]}]\n\n',
        'event: messages\ndata: [{"content":[],"type":"AIMessageChunk","tool_call_chunks":[{"index":0,"args":",\\"top_k\\":3}"}]}]\n\n',
        `event: messages/complete\ndata: [{"content":"router: agent","type":"tool","role":"tool","tool_call_id":"${MOCK_PLAYGROUND_TOOL_ID}","name":"classify_query","status":"success"}]\n\n`,
        'event: messages\ndata: [{"content":"Agent ","type":"AIMessageChunk","role":"assistant"}]\n\n',
        'event: messages\ndata: [{"content":"native ","type":"AIMessageChunk","role":"assistant"}]\n\n',
        'event: messages\ndata: [{"content":"response","type":"AIMessageChunk","role":"assistant"}]\n\n',
        'event: messages/complete\ndata: [{"content":"Agent native response","type":"ai","role":"assistant"}]\n\n',
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
        content: "router: agent",
      },
      {
        type: "AIMessage",
        role: "assistant",
        content: "Agent native response",
        tool_calls: [
          {
            id: MOCK_PLAYGROUND_TOOL_ID,
            name: "classify_query",
            args: { query: "agent" },
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

  await expect(page.getByText("Agent native response")).toBeVisible();
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
