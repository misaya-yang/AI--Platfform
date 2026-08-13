import { expect, test, type APIRequestContext } from "@playwright/test";
import { buildAuthHeaders, loginThroughApi } from "./support/helpers";

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

async function installApiSession(page: import("@playwright/test").Page, request: APIRequestContext) {
  const { token, user } = await loginThroughApi(request);
  await page.addInitScript(
    ({ authPayload }) => {
      localStorage.setItem("agent-gateway-auth", JSON.stringify(authPayload));
      sessionStorage.removeItem("agent-gateway-auth");
    },
    {
      authPayload: {
        state: {
          token,
          user,
          isAuthenticated: true,
          forcePasswordChange: false,
          rememberMe: true,
        },
        version: 0,
      },
    }
  );
  await page.route(/\/api\/v1\//, async (route) => {
    const requestUrl = new URL(route.request().url());
    const response = await request.fetch(`${process.env.E2E_API_URL}${requestUrl.pathname}${requestUrl.search}`, {
      headers: {
        ...route.request().headers(),
        authorization: `Bearer ${token}`,
      },
      method: route.request().method(),
      data: route.request().postDataBuffer(),
    });
    await route.fulfill({ response });
  });
}

test("assistant restores seeded history and keeps sidebar toggle functional", async ({ page, request }) => {
  const { title } = await seedAssistantSession(request);

  await installApiSession(page, request);
  await page.goto("/assistant");
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

  await installApiSession(page, request);
  await page.goto("/assistant");
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
