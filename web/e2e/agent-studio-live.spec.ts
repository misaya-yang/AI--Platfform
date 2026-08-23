import fs from "node:fs/promises";
import path from "node:path";

import { expect, test, type Page } from "@playwright/test";

const liveEnabled =
  process.env.E2E_LIVE_AGENT_STUDIO === "1" &&
  process.env.E2E_DOCKER_LIVE_STACK === "1";
const liveDisabledEnabled = process.env.E2E_LIVE_AGENT_STUDIO_DISABLED === "1";

async function deleteCreatedAgent(page: Page, agentId: string): Promise<void> {
  const responseStatus = await page.evaluate(async (id) => {
    const rawAuth = localStorage.getItem("agent-gateway-auth");
    const token = rawAuth
      ? (JSON.parse(rawAuth) as { state?: { token?: string } }).state?.token
      : undefined;
    if (!token) return 0;
    const response = await fetch(`/api/v1/agents/${id}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
    return response.status;
  }, agentId);
  expect(responseStatus).toBe(200);
}

test.describe("Agent Studio live stack", () => {
  test.skip(
    !liveEnabled,
    "Use playwright.live.config.ts with E2E_LIVE_AGENT_STUDIO=1 against the authenticated Docker stack.",
  );

  test("creates, saves, previews, renders responsively, and cleans up a real Draft", async ({ page }) => {
    page.setDefaultTimeout(15_000);
    await page.addInitScript(() => localStorage.setItem("i18nextLng", "en-US"));
    const evidenceDir = path.resolve(process.cwd(), "../reports/agent-studio/as-05");
    await fs.mkdir(evidenceDir, { recursive: true });
    const uniqueName = `AS05 Live ${Date.now()}`;
    const savedName = `${uniqueName} updated`;
    const savedDescription = "Persists Agent metadata and Draft spec in one live transaction.";
    let agentId: string | null = null;

    try {
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto("/agents", { waitUntil: "domcontentloaded" });
      await expect(page.getByTestId("agents-page")).toBeVisible();
      await page.getByRole("button", { name: "Create agent" }).click();
      await page.getByLabel("Name").fill(uniqueName);
      await page.getByLabel("Description").fill("Validates the real AS-05 local Agent Studio runtime path.");
      await page.getByRole("button", { name: "Continue" }).click();
      await expect(page.getByRole("region", { name: "Behavior" })).toBeVisible();
      await page.getByRole("button", { name: "Continue" }).click();
      await expect(page.getByRole("heading", { name: "Start" })).toBeVisible();
      await page.getByRole("button", { name: "Create agent" }).click();
      await expect(page).toHaveURL(/\/agents\/[0-9a-f-]{36}$/);
      agentId = new URL(page.url()).pathname.split("/").pop() || null;
      expect(agentId).toBeTruthy();

      await expect(page.getByRole("heading", { name: uniqueName })).toBeVisible();
      await expect(page.getByText(/Draft · revision \d+/)).toBeVisible();
      await page.getByLabel("Name").fill(savedName);
      await page.getByLabel("Description").fill(savedDescription);
      await page.getByLabel("Welcome message").fill("Welcome to the AS-05 live Preview.");
      const saveDraftButton = page.locator(".agent-studio-actions").getByRole("button", { name: "Save draft" });
      await expect(saveDraftButton).toBeEnabled();
      await saveDraftButton.click();
      await expect(page.locator(".agent-save-state")).toHaveText("Saved");
      await expect(saveDraftButton).toBeDisabled();
      await page.reload({ waitUntil: "domcontentloaded" });
      await expect(page.getByRole("heading", { name: savedName })).toBeVisible();
      await expect(page.getByLabel("Description")).toHaveValue(savedDescription);
      await expect(page.getByLabel("Welcome message")).toHaveValue("Welcome to the AS-05 live Preview.");
      await page.locator(".agent-preview-header").getByRole("button", { name: "New session" }).click();
      const startedSession = page.getByText(/New isolated session · Draft r\d+/);
      await expect(startedSession).toBeVisible();
      await page.getByLabel("Message this agent").fill("Explain why Transformer training can be parallelized in three sentences.");
      await page.getByLabel("Send Preview message").click();
      const assistantResponse = page.locator(".agent-preview-message-assistant p").last();
      await expect(assistantResponse).toBeVisible();
      await expect(assistantResponse).not.toHaveText(/Generating|completed without a text response/);
      await expect(assistantResponse).not.toHaveText("Agent E2E stub response");
      await expect(page.locator(".agent-preview-error")).toHaveCount(0);
      await page.screenshot({
        path: path.join(evidenceDir, "studio-desktop.png"),
        fullPage: true,
      });

      await page.setViewportSize({ width: 1024, height: 768 });
      await expect(page.locator(".agent-config-canvas")).toBeVisible();
      await expect(page.getByTestId("agent-preview-panel")).toBeVisible();
      await page.screenshot({
        path: path.join(evidenceDir, "studio-tablet.png"),
        fullPage: true,
      });

      await page.setViewportSize({ width: 390, height: 844 });
      await page.getByRole("tab", { name: "Preview" }).click();
      await expect(page.getByTestId("agent-preview-panel")).toBeVisible();
      await expect(page.locator(".agent-config-canvas")).toBeHidden();
      await page.screenshot({
        path: path.join(evidenceDir, "studio-mobile.png"),
        fullPage: true,
      });
    } finally {
      if (agentId) await deleteCreatedAgent(page, agentId);
    }
  });
});

test.describe("Agent Studio live rollback flag", () => {
  test.skip(
    !liveDisabledEnabled,
    "Set E2E_LIVE_AGENT_STUDIO_DISABLED=1 after starting the frontend with the flag disabled.",
  );

  test("removes Agent surfaces while preserving the existing Assistant", async ({ page }) => {
    await page.goto("/agents", { waitUntil: "domcontentloaded" });
    await expect(page.getByRole("link", { name: "Agents" })).toHaveCount(0);
    await expect(page.getByTestId("agents-page")).toHaveCount(0);

    await page.goto("/assistant", { waitUntil: "domcontentloaded" });
    await expect(page.locator("#assistant-chat-composer")).toBeVisible();
    await expect(page).toHaveURL(/\/assistant$/);
  });
});
