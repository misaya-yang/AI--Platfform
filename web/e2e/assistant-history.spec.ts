import { expect, test, type APIRequestContext } from "@playwright/test";
import { buildAuthHeaders, ensureAuthenticatedPage } from "./support/helpers";

function sessionButtonName(title: string): RegExp {
  return new RegExp(`^${title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}(?:\\s·|$)`);
}

function historyToggle(page: import("@playwright/test").Page, state: "show" | "hide") {
  const labels = state === "show"
    ? ["Show history", "显示历史"]
    : ["Hide history", "隐藏历史"];
  return page.locator(labels.map((label) => `button[aria-label="${label}"]`).join(", "));
}

async function seedAssistantSession(request: APIRequestContext) {
  const headers = await buildAuthHeaders(request);
  const title = `assistant-history-${Date.now()}`;
  const createResponse = await request.post(`${process.env.E2E_API_URL}/api/v1/sessions`, {
    headers,
    data: {
      service_id: "__builtin_assistant__",
      metadata: { title },
    },
  });
  expect(createResponse.ok()).toBeTruthy();
  const { session_id: sessionId } = (await createResponse.json()) as { session_id: string };

  await request.post(`${process.env.E2E_API_URL}/api/v1/sessions/${sessionId}/messages`, {
    headers,
    data: { role: "user", content: "History seed question" },
  });
  await request.post(`${process.env.E2E_API_URL}/api/v1/sessions/${sessionId}/messages`, {
    headers,
    data: { role: "assistant", content: "History seed answer" },
  });

  return { sessionId, title };
}

test("assistant restores seeded history and keeps sidebar toggle functional", async ({ page, request }) => {
  const { title } = await seedAssistantSession(request);

  await ensureAuthenticatedPage(page, "/assistant");
  const sessionButton = page.getByRole("button", { name: sessionButtonName(title) });
  if (!(await sessionButton.isVisible())) {
    await historyToggle(page, "show").click();
  }
  await sessionButton.click();

  await expect(page.getByText("History seed question")).toBeVisible();
  await expect(page.getByText("History seed answer")).toBeVisible();

  const toggle = historyToggle(page, "hide");
  await toggle.click();
  await expect(historyToggle(page, "show")).toBeVisible();

  await page.reload();
  await expect(page.getByText("History seed question")).toBeVisible();
  await expect(historyToggle(page, "show")).toBeVisible();
});

test("assistant restores seeded history in the mobile history sheet", async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { title } = await seedAssistantSession(request);

  await ensureAuthenticatedPage(page, "/assistant");
  await historyToggle(page, "show").click();
  const historySheet = page.getByRole("dialog", { name: /history|历史/i });
  await expect(historySheet).toBeVisible();
  const bounds = await historySheet.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.width).toBeLessThanOrEqual(390);

  await page.getByRole("button", { name: sessionButtonName(title) }).click();
  await expect(page.getByText("History seed question")).toBeVisible();
  await expect(page.getByText("History seed answer")).toBeVisible();
});
