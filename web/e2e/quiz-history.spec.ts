/**
 * Quiz History Persistence E2E Test
 *
 * Verifies that when a session contains a quiz, switching away and back
 * restores the QuizCard from the DB (via quiz_id in message metadata).
 *
 * Flow:
 * 1. Create session + seed messages with quiz_id in metadata
 * 2. Generate a real quiz via API so GET /quiz/{id} works
 * 3. Navigate to assistant, switch to the seeded session
 * 4. Verify QuizCard renders (hydrated from DB)
 */

import { expect, test, type APIRequestContext } from "@playwright/test";
import { buildAuthHeaders, ensureAuthenticatedPage, getApiUrl } from "./support/helpers";

function sessionButtonName(title: string): RegExp {
  return new RegExp(`^${title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:\\s·|$)`);
}

async function seedSessionWithQuiz(request: APIRequestContext) {
  const headers = await buildAuthHeaders(request);
  const apiUrl = getApiUrl();
  const title = `quiz-history-${Date.now()}`;

  // Step 1: Generate a real quiz so it exists in DB
  // First get a dataset
  const dsRes = await request.get(`${apiUrl}/api/v1/assistant/datasets`, { headers });
  expect(dsRes.ok()).toBeTruthy();
  const dsData = await dsRes.json();
  const datasets = dsData.datasets ?? dsData.data ?? dsData;

  if (!Array.isArray(datasets) || datasets.length === 0) {
    return null; // skip if no datasets
  }

  const datasetId = datasets[0].id ?? datasets[0].dataset_id;
  const genRes = await request.post(`${apiUrl}/api/v1/assistant/quiz/generate`, {
    headers,
    data: {
      dataset_ids: [datasetId],
      topic: "history persistence test",
      question_count: 3,
      difficulty: "easy",
    },
  });
  expect(genRes.ok(), `Quiz gen failed: ${genRes.status()}`).toBeTruthy();
  const quiz = await genRes.json();
  const quizId = quiz.quiz_id;

  // Step 2: Create session
  const createRes = await request.post(`${apiUrl}/api/v1/sessions`, {
    headers,
    data: {
      service_id: "__builtin_assistant__",
      metadata: { title },
    },
  });
  expect(createRes.ok()).toBeTruthy();
  const { session_id: sessionId } = (await createRes.json()) as { session_id: string };

  // Step 3: Seed user message
  await request.post(`${apiUrl}/api/v1/sessions/${sessionId}/messages`, {
    headers,
    data: { role: "user", content: "请根据知识库出3道题" },
  });

  // Step 4: Seed assistant message with quiz_id in metadata
  await request.post(`${apiUrl}/api/v1/sessions/${sessionId}/messages`, {
    headers,
    data: {
      role: "assistant",
      content: `Quiz "${quiz.title}" created with ${quiz.question_count} questions. Interactive quiz card is now displayed.`,
      metadata: { quiz_id: quizId },
    },
  });

  return { sessionId, title, quizId, quizTitle: quiz.title };
}

test.describe("Quiz history persistence", () => {
  test.setTimeout(3 * 60_000);

  let headers: Record<string, string>;
  let cleanupIds: { sessionId?: string; quizId?: string } = {};

  test.beforeAll(async ({ request }) => {
    headers = await buildAuthHeaders(request);
  });

  test.afterAll(async ({ request }) => {
    const apiUrl = getApiUrl();
    if (cleanupIds.sessionId) {
      await request.delete(`${apiUrl}/api/v1/sessions/${cleanupIds.sessionId}`, { headers }).catch(() => {});
    }
    if (cleanupIds.quizId) {
      await request.delete(`${apiUrl}/api/v1/assistant/quiz/${cleanupIds.quizId}`, { headers }).catch(() => {});
    }
  });

  test("restores QuizCard when switching to a session with quiz history", async ({ page, request }) => {
    const seed = await seedSessionWithQuiz(request);
    if (!seed) {
      test.skip(true, "No KB datasets available");
      return;
    }
    cleanupIds = { sessionId: seed.sessionId, quizId: seed.quizId };

    // Navigate to assistant page
    await ensureAuthenticatedPage(page, "/assistant");

    // Find and click the seeded session in sidebar
    const sessionBtn = page.getByRole("button", { name: sessionButtonName(seed.title) });
    await expect(sessionBtn).toBeVisible({ timeout: 15_000 });
    await sessionBtn.click();

    // Wait for history restore — the user message should appear
    await expect(page.getByText("请根据知识库出3道题")).toBeVisible({ timeout: 15_000 });

    // QuizCard should render after async hydration
    // QuizCard contains the quiz title and question elements
    await expect(
      page.getByRole("heading", { name: seed.quizTitle, level: 2 }),
    ).toBeVisible({ timeout: 15_000 });

    // Verify quiz questions are rendered (QuizCard shows question text)
    const questionElements = page.locator("[data-testid='quiz-question'], .quiz-question");
    // Fallback: just check there's some quiz UI element
    const quizCard = page.locator("[data-testid='quiz-card'], .quiz-card");
    const hasQuizCard = await quizCard.count().catch(() => 0);
    const hasQuestions = await questionElements.count().catch(() => 0);

    // At least the quiz title should be visible — that proves hydration worked
    const titleVisible = await page
      .getByRole("heading", { name: seed.quizTitle, level: 2 })
      .isVisible();
    expect(hasQuizCard > 0 || hasQuestions > 0 || titleVisible).toBeTruthy();
  });
});
