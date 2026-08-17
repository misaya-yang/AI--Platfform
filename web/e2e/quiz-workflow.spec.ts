/**
 * Quiz Workflow E2E Test
 *
 * Covers the Phase 1 quiz lifecycle:
 * 1. Generate quiz via API
 * 2. Verify quiz structure (questions, options)
 * 3. Submit answers and verify grading
 * 4. List quizzes and verify the created quiz appears
 * 5. Delete quiz and verify removal
 */

import { expect, test } from "@playwright/test";
import { buildAuthHeaders, getApiUrl } from "./support/helpers";

test.describe("Quiz workflow", () => {
  test.setTimeout(3 * 60_000);

  let headers: Record<string, string>;
  let quizId: string | undefined;
  let sessionId: string | undefined;

  test.beforeAll(async ({ request }) => {
    headers = await buildAuthHeaders(request);
  });

  test.afterAll(async ({ request }) => {
    // Cleanup: delete quiz if it was created and not already deleted
    if (quizId) {
      const apiUrl = getApiUrl();
      await request
        .delete(`${apiUrl}/api/v1/assistant/quiz/${quizId}`, { headers })
        .catch(() => {});
    }
    if (sessionId) {
      const apiUrl = getApiUrl();
      await request
        .delete(`${apiUrl}/api/v1/sessions/${sessionId}`, { headers })
        .catch(() => {});
    }
  });

  test("generate, submit, share, public access, delete quiz", async ({ page, request }) => {
    const apiUrl = getApiUrl();

    // --- Step 1: List KB datasets to find one with content ---
    const dsRes = await request.get(`${apiUrl}/api/v1/assistant/datasets`, {
      headers,
    });
    expect(dsRes.ok(), `List datasets failed: ${dsRes.status()}`).toBeTruthy();
    const dsData = await dsRes.json();
    const datasets = dsData.datasets ?? dsData.data ?? dsData;
    expect(Array.isArray(datasets)).toBeTruthy();

    // Skip test if no datasets available
    if (datasets.length === 0) {
      test.skip(true, "No KB datasets available for quiz generation");
      return;
    }

    const datasetId = datasets[0].id ?? datasets[0].dataset_id;
    expect(datasetId).toBeTruthy();

    // --- Step 2: Generate a quiz through the assistant (in-chat generate_quiz tool) ---
    // The gateway quiz-generation API was removed (PC-03); the supported path is
    // the in-chat tool. Ask the assistant for a quiz and capture the quiz id from
    // the hydration GET the quiz card fires. Requires a live provider.
    await page.goto("/assistant", { waitUntil: "domcontentloaded" });
    const composer = page.locator("#assistant-chat-composer");
    await expect(composer).toBeVisible();

    // Bind the dataset through the same UI state used by real chat requests.
    // Merely discovering a dataset through the API does not attach it to the
    // conversation, and the assistant correctly refuses KB-grounded quiz
    // generation when ``kb_dataset_ids`` is empty.
    const composerShell = composer.locator("xpath=..");
    await composerShell.locator("button:has(svg.lucide-plus)").click();
    await page
      .getByRole("button", { name: /Company Knowledge Base|公司知识库/ })
      .click();
    const datasetName = String(datasets[0].name ?? "");
    expect(datasetName).toBeTruthy();
    await page.getByText(datasetName, { exact: true }).click();
    await composer.click();

    const chatRequest = page.waitForRequest(
      (req) =>
        req.method() === "POST" &&
        new URL(req.url()).pathname.endsWith("/api/v1/assistant/chat/stream"),
    );
    await composer.fill("请根据知识库出 4 道单选题，覆盖核心概念");
    await composer.press("Enter");

    const chatPayload = (await chatRequest).postDataJSON();
    expect(chatPayload.kb_dataset_ids).toContain(datasetId);
    sessionId = String(chatPayload.session_id ?? "");
    expect(sessionId).toBeTruthy();

    // A freshly streamed quiz is already hydrated from the canonical
    // ``quiz:ready`` SSE payload, so no GET is expected at render time.
    // Verify the card and resolve its id from persisted session metadata.
    await expect(
      page.getByRole("button", { name: /Start quiz|开始作答/ }),
    ).toBeVisible({ timeout: 180_000 });
    await expect
      .poll(
        async () => {
          const historyRes = await request.get(
            `${apiUrl}/api/v1/sessions/${sessionId}/history`,
            { headers },
          );
          if (!historyRes.ok()) return undefined;
          const messages = await historyRes.json();
          const quizMessage = [...messages]
            .reverse()
            .find(
              (message) =>
                message.role === "assistant" &&
                typeof message.metadata?.quiz_id === "string",
            );
          quizId = quizMessage?.metadata?.quiz_id;
          return quizId;
        },
        { timeout: 30_000 },
      )
      .toMatch(/^[0-9a-f-]{36}$/);
    expect(quizId).toBeTruthy();

    // Verify question structure
    const getRes = await request.get(
      `${apiUrl}/api/v1/assistant/quiz/${quizId}`,
      { headers },
    );
    expect(getRes.ok()).toBeTruthy();
    const quiz = await getRes.json();
    expect(quiz.quiz_id).toBe(quizId);
    expect(quiz.title).toBeTruthy();
    expect(quiz.questions).toBeDefined();
    expect(quiz.questions.length).toBeGreaterThanOrEqual(1);
    const firstQ = quiz.questions[0];
    expect(firstQ.id).toBeTruthy();
    expect(firstQ.question_text).toBeTruthy();
    expect(firstQ.options).toBeDefined();
    expect(firstQ.options.length).toBe(4);
    expect(firstQ.options[0]).toHaveProperty("label");
    expect(firstQ.options[0]).toHaveProperty("text");

    // Correct answers should NOT be included
    expect(firstQ.correct_answer).toBeUndefined();

    // --- Step 3: Submit answers ---
    const answers: Record<string, string> = {};
    for (const q of quiz.questions) {
      // Pick "A" for all (deterministic for test — we just verify grading works)
      answers[q.id] = "A";
    }

    const submitRes = await request.post(
      `${apiUrl}/api/v1/assistant/quiz/${quizId}/submit`,
      {
        headers,
        data: { answers },
      },
    );
    expect(
      submitRes.ok(),
      `Submit failed: ${submitRes.status()}`,
    ).toBeTruthy();

    const result = await submitRes.json();
    expect(result.attempt_id).toBeTruthy();
    expect(typeof result.total_score).toBe("number");
    expect(result.correct_count).toBeGreaterThanOrEqual(0);
    expect(result.total_count).toBe(quiz.questions.length);
    expect(result.per_question).toBeDefined();
    expect(result.per_question.length).toBe(quiz.questions.length);

    // Verify per-question structure
    const pq = result.per_question[0];
    expect(pq).toHaveProperty("correct");
    expect(pq).toHaveProperty("user_answer");
    expect(pq).toHaveProperty("correct_answer");
    expect(pq).toHaveProperty("explanation");

    // --- Step 4: Create share link (kind-generic artifact share) ---
    const shareRes = await request.post(
      `${apiUrl}/api/v1/artifact-shares`,
      {
        headers,
        data: { kind: "quiz", quiz_id: quizId, require_name: true },
      },
    );
    expect(shareRes.ok(), `Create share failed: ${shareRes.status()}`).toBeTruthy();
    const share = await shareRes.json();
    expect(share.share_code).toBeTruthy();
    expect(share.quiz_id).toBe(quizId);

    // --- Step 5: Access shared quiz (no auth) ---
    const publicRes = await request.get(
      `${apiUrl}/api/v1/quiz/shared/${share.share_code}`,
    );
    expect(publicRes.ok(), `Public quiz access failed: ${publicRes.status()}`).toBeTruthy();
    const publicQuiz = await publicRes.json();
    expect(publicQuiz.title).toBeTruthy();
    expect(publicQuiz.questions.length).toBeGreaterThanOrEqual(1);
    // Verify no correct answers exposed
    expect(publicQuiz.questions[0].correct_answer).toBeUndefined();

    // --- Step 6: Submit anonymous attempt ---
    const publicAnswers: Record<string, string> = {};
    for (const q of publicQuiz.questions) {
      publicAnswers[q.id] = "B";
    }
    const publicSubmitRes = await request.post(
      `${apiUrl}/api/v1/quiz/shared/${share.share_code}/submit`,
      {
        data: {
          answers: publicAnswers,
          display_name: "E2E Test User",
        },
      },
    );
    expect(publicSubmitRes.ok(), `Public submit failed: ${publicSubmitRes.status()}`).toBeTruthy();
    const publicResult = await publicSubmitRes.json();
    expect(publicResult.attempt_id).toBeTruthy();
    expect(publicResult.total_count).toBe(publicQuiz.questions.length);

    // --- Step 7: Navigate to /quiz/:shareCode page ---
    // Just verify the public page loads (renders intro screen)
    await page.goto(`/quiz/${share.share_code}`, { waitUntil: "domcontentloaded" });
    await expect(
      page.getByRole("heading", { name: publicQuiz.title }).first(),
    ).toBeVisible({ timeout: 10_000 });

    // --- Step 8: Delete quiz ---
    const delRes = await request.delete(
      `${apiUrl}/api/v1/assistant/quiz/${quizId}`,
      { headers },
    );
    expect(delRes.ok(), `Delete failed: ${delRes.status()}`).toBeTruthy();
    const deletedQuizId = quizId;
    quizId = undefined; // prevent afterAll cleanup

    // Verify deletion
    const getAfterDel = await request.get(
      `${apiUrl}/api/v1/assistant/quiz/${deletedQuizId}`,
      { headers },
    );
    expect(getAfterDel.status()).toBe(404);
  });
});
