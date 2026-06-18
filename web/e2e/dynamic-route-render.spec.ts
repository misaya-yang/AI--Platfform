import { expect, test, type Page } from "@playwright/test";
import { installClientAuth } from "./support/helpers";

const allPagePermissions = [
  "console:dashboard:view",
  "console:services:view",
  "knowledge:dataset:view",
  "knowledge:dataset:create",
  "conversation:playground:access",
  "console:settings:view",
  "user:list",
  "user:edit",
];

function jsonResponse(body: unknown) {
  return {
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

function nowIso() {
  return new Date("2026-06-17T08:00:00.000Z").toISOString();
}

async function routeJson(page: Page, url: string, body: unknown) {
  await page.route(url, async (route) => {
    await route.fulfill(jsonResponse(body));
  });
}

async function installDynamicRouteHarness(page: Page) {
  await installClientAuth(page, {
    user_id: "dynamic-route-user",
    email: "dynamic-routes@example.com",
    display_name: "Dynamic Route User",
    permissions: allPagePermissions,
    effective_permissions: allPagePermissions,
  });

  await page.route("**/api/v1/knowledge/**", async (route) => {
    const request = route.request();
    if (request.method() === "GET") {
      await route.fulfill(jsonResponse([]));
      return;
    }
    await route.fallback();
  });

  await routeJson(page, "**/api/v1/knowledge/datasets/demo-kb-ai-gateway", {
    dataset_id: "demo-kb-ai-gateway",
    name: "AI Gateway Demo Knowledge Base",
    description: "Small local knowledge base used by the open-source quickstart.",
    visibility: "tenant",
    kb_type: "document",
    use_case: "basic_qa",
    embedding_provider: "openai",
    embedding_model: "text-embedding-3-small",
    embedding_dimension: 1536,
    statistics: {
      document_count: 1,
      segment_count: 1,
      token_count: 42,
    },
    created_at: nowIso(),
    updated_at: nowIso(),
  });

  await routeJson(page, "**/api/v1/knowledge/demo-kb-ai-gateway/documents", [
    {
      document_id: "demo-doc-quickstart-runbook",
      dataset_id: "demo-kb-ai-gateway",
      title: "Local Quickstart Runbook",
      status: "completed",
      segment_count: 1,
      word_count: 12,
      char_count: 84,
      enabled: true,
      source_type: "upload",
      created_at: nowIso(),
      updated_at: nowIso(),
    },
  ]);

  await routeJson(page, "**/api/v1/knowledge/demo-kb-ai-gateway/segments**", [
    {
      segment_id: "demo-seg-quickstart-001",
      dataset_id: "demo-kb-ai-gateway",
      document_id: "demo-doc-quickstart-runbook",
      position: 1,
      text: "AI Gateway is a local-first open-source platform for routing AI providers, managing assistant sessions, and testing knowledge-base retrieval.",
      enabled: true,
      status: "completed",
      token_count: 12,
      char_count: 64,
      created_at: nowIso(),
    },
  ]);

  await routeJson(page, "**/api/v1/exams/00000000-0000-4000-8000-000000000044", {
    exam_id: "00000000-0000-4000-8000-000000000044",
    quiz_id: "00000000-0000-4000-8000-000000000041",
    title: "AI Gateway Demo Exam",
    description: "A published exam record for local route smoke checks.",
    status: "published",
    published_by: "dynamic-route-user",
    question_count: 1,
    attempt_count: 0,
    avg_score: null,
    deadline: null,
    max_retakes: 1,
    time_limit_minutes: null,
    passing_score: 0.6,
    share_id: "00000000-0000-4000-8000-000000000043",
    share_code: "demo-quiz",
    created_at: nowIso(),
    updated_at: nowIso(),
  });

  await routeJson(page, "**/api/v1/exams/00000000-0000-4000-8000-000000000044/attempts**", {
    attempts: [],
    total: 0,
  });

  await routeJson(page, "**/api/v1/exams/00000000-0000-4000-8000-000000000044/stats", {
    total_participants: 0,
    avg_score: null,
    min_score: null,
    max_score: null,
    passed: 0,
    pass_rate: 0,
    passing_score: 0.6,
    score_distribution: {},
    per_question: [
      {
        question_num: 1,
        question_text: "Which route family is under smoke coverage?",
        correct_answer: "A",
        correct_rate: 0,
        total_answered: 0,
        most_common_wrong: null,
      },
    ],
  });

  await routeJson(page, "**/api/v1/exams/00000000-0000-4000-8000-000000000044/reports", {
    reports: [],
  });

  await routeJson(page, "**/api/v1/assistant/shares/demo-share", {
    share_code: "demo-share",
    title: "Open-source demo conversation",
    snapshot: {
      model_id: "gpt-4o",
      shared_at: nowIso(),
      artifacts: [],
      messages: [
        {
          role: "user",
          content: "What can this platform do?",
          timestamp: nowIso(),
        },
        {
          role: "assistant",
          content: "It routes AI providers, serves a general assistant, and exposes knowledge-base workflows through a local gateway.",
          timestamp: nowIso(),
        },
      ],
    },
    message_count: 2,
    artifact_count: 0,
    view_count: 1,
    created_at: nowIso(),
    expires_at: null,
  });

  await routeJson(page, "**/api/v1/quiz/shared/demo-quiz", {
    quiz_id: "00000000-0000-4000-8000-000000000041",
    share_code: "demo-quiz",
    title: "AI Gateway Demo Quiz",
    description: "A one-question quiz that proves public quiz routes can render after seeding.",
    question_count: 1,
    difficulty: "easy",
    require_name: false,
    questions: [
      {
        id: "00000000-0000-4000-8000-000000000042",
        question_num: 1,
        question_type: "mc_single",
        question_text: "Which route should render after loading the open-source demo data?",
        options: [
          { label: "A", text: "/quiz/demo-quiz" },
          { label: "B", text: "A settings page" },
        ],
      },
    ],
  });

}

function watchRuntimeFailures(page: Page) {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const badResponses: string[] = [];

  page.on("console", (message) => {
    if (message.type() === "error" && !/favicon|NO_COLOR/i.test(message.text())) {
      consoleErrors.push(message.text());
    }
  });
  page.on("pageerror", (error) => {
    pageErrors.push(String(error));
  });
  page.on("response", (response) => {
    const url = response.url();
    if (response.status() >= 400 && /\/api\/v1\//.test(url)) {
      badResponses.push(`${response.status()} ${url}`);
    }
  });

  return () => {
    expect(pageErrors, `Page runtime errors:\n${pageErrors.join("\n")}`).toEqual([]);
    expect(consoleErrors, `Console errors:\n${consoleErrors.join("\n")}`).toEqual([]);
    expect([...new Set(badResponses)], `API responses >= 400:\n${badResponses.join("\n")}`).toEqual([]);
  };
}

test.describe("dynamic route render smoke", () => {
  test("renders protected dynamic knowledge and exam routes with seeded API responses", async ({
    page,
  }) => {
    const assertNoRuntimeFailures = watchRuntimeFailures(page);
    await installDynamicRouteHarness(page);

    await page.goto("/knowledge/demo-kb-ai-gateway", { waitUntil: "domcontentloaded" });
    await expect(page.getByText("AI Gateway Demo Knowledge Base")).toBeVisible();
    await expect(page.getByText("Local Quickstart Runbook")).toBeVisible();

    await page.goto("/exams/00000000-0000-4000-8000-000000000044", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "AI Gateway Demo Exam" })).toBeVisible();
    await expect(page.getByText(/0 人参与|暂无考生|No participants/i).first()).toBeVisible();

    assertNoRuntimeFailures();
  });

  test("renders public share and quiz routes with seeded API responses", async ({ page }) => {
    const assertNoRuntimeFailures = watchRuntimeFailures(page);
    await installDynamicRouteHarness(page);
    await page.addInitScript(() => {
      localStorage.removeItem("quiz_submitted_demo-quiz");
    });

    await page.goto("/share/demo-share", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "Open-source demo conversation" })).toBeVisible();
    await expect(page.getByText("It routes AI providers, serves a general assistant")).toBeVisible();

    await page.goto("/quiz/demo-quiz", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "AI Gateway Demo Quiz" }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: /start quiz/i })).toBeEnabled();

    assertNoRuntimeFailures();
  });
});
