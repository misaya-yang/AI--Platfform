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

  await routeJson(page, "**/api/v1/knowledge/datasets/ds-dynamic-smoke", {
    dataset_id: "ds-dynamic-smoke",
    name: "Dynamic Knowledge Smoke",
    description: "Seedless route smoke dataset",
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

  await routeJson(page, "**/api/v1/knowledge/ds-dynamic-smoke/documents", [
    {
      document_id: "doc-dynamic-smoke",
      dataset_id: "ds-dynamic-smoke",
      title: "Dynamic route source note",
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

  await routeJson(page, "**/api/v1/knowledge/ds-dynamic-smoke/segments**", [
    {
      segment_id: "seg-dynamic-smoke",
      dataset_id: "ds-dynamic-smoke",
      document_id: "doc-dynamic-smoke",
      position: 1,
      text: "Dynamic route rendering should work when a dataset record exists.",
      enabled: true,
      status: "completed",
      token_count: 12,
      char_count: 64,
      created_at: nowIso(),
    },
  ]);

  await routeJson(page, "**/api/v1/exams/exam-dynamic-smoke", {
    exam_id: "exam-dynamic-smoke",
    quiz_id: "quiz-dynamic-smoke",
    title: "Dynamic Exam Smoke",
    description: "Seedless route smoke exam",
    status: "published",
    published_by: "dynamic-route-user",
    question_count: 1,
    attempt_count: 0,
    avg_score: null,
    deadline: null,
    max_retakes: 1,
    time_limit_minutes: null,
    passing_score: 0.6,
    share_id: "share-dynamic-smoke",
    share_code: "quiz-dynamic-smoke",
    created_at: nowIso(),
    updated_at: nowIso(),
  });

  await routeJson(page, "**/api/v1/exams/exam-dynamic-smoke/attempts**", {
    attempts: [],
    total: 0,
  });

  await routeJson(page, "**/api/v1/exams/exam-dynamic-smoke/stats", {
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

  await routeJson(page, "**/api/v1/exams/exam-dynamic-smoke/reports", {
    reports: [],
  });

  await routeJson(page, "**/api/v1/assistant/shares/share-dynamic-smoke", {
    share_code: "share-dynamic-smoke",
    title: "Dynamic Share Smoke",
    snapshot: {
      model_id: "gpt-4o",
      shared_at: nowIso(),
      artifacts: [],
      messages: [
        {
          role: "user",
          content: "Can this shared conversation render?",
          timestamp: nowIso(),
        },
        {
          role: "assistant",
          content: "Yes. This public share route renders from mocked seed data.",
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

  await routeJson(page, "**/api/v1/quiz/shared/quiz-dynamic-smoke", {
    quiz_id: "quiz-dynamic-smoke",
    share_code: "quiz-dynamic-smoke",
    title: "Dynamic Quiz Smoke",
    description: "Seedless public quiz route smoke",
    question_count: 1,
    difficulty: "easy",
    require_name: false,
    questions: [
      {
        id: "q-dynamic-smoke",
        question_num: 1,
        question_type: "mc_single",
        question_text: "Which page is this?",
        options: [
          { label: "A", text: "A public quiz route" },
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

    await page.goto("/knowledge/ds-dynamic-smoke", { waitUntil: "domcontentloaded" });
    await expect(page.getByText("Dynamic Knowledge Smoke")).toBeVisible();
    await expect(page.getByText("Dynamic route source note")).toBeVisible();

    await page.goto("/exams/exam-dynamic-smoke", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "Dynamic Exam Smoke" })).toBeVisible();
    await expect(page.getByText(/0 人参与|暂无考生|No participants/i).first()).toBeVisible();

    assertNoRuntimeFailures();
  });

  test("renders public share and quiz routes with seeded API responses", async ({ page }) => {
    const assertNoRuntimeFailures = watchRuntimeFailures(page);
    await installDynamicRouteHarness(page);
    await page.addInitScript(() => {
      localStorage.removeItem("quiz_submitted_quiz-dynamic-smoke");
    });

    await page.goto("/share/share-dynamic-smoke", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "Dynamic Share Smoke" })).toBeVisible();
    await expect(page.getByText("This public share route renders from mocked seed data.")).toBeVisible();

    await page.goto("/quiz/quiz-dynamic-smoke", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("heading", { name: "Dynamic Quiz Smoke" }).first()).toBeVisible();
    await expect(page.getByRole("button", { name: /start quiz/i })).toBeEnabled();

    assertNoRuntimeFailures();
  });
});
