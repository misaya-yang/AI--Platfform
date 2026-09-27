/** Real saved PDF + explicitly controlled image-history recovery. No provider calls. */
import fs from "node:fs/promises";
import { createHash } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { buildAuthHeaders, ensureAuthenticatedPage } from "./support/helpers";

// Full Chromium includes its PDF viewer; the headless shell only downloads PDFs.
test.use({ channel: "chromium" });

async function openHistory(page: Page, title: string, searchText: string) {
  await ensureAuthenticatedPage(page, "/assistant");
  const search = page.getByPlaceholder(/Search conversations|搜索对话/);
  if (!(await search.isVisible())) await page.getByRole("button", { name: /^(Show history|显示历史)$/ }).first().click();
  await search.fill(searchText);
  await page.getByRole("button", { name: title, exact: true }).click();
}

test("real saved PDF opens authenticated preview and downloads identical bytes at 390px", async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const headers = await buildAuthHeaders(request);
  const sessions = await request.get(`${process.env.E2E_API_URL}/api/v1/assistant/sessions?limit=200`, { headers });
  expect(sessions.ok()).toBeTruthy();
  const session = (await sessions.json()).sessions.find((s: { metadata?: { title?: string } }) => s.metadata?.title?.startsWith("R1-PDF-927"));
  expect(session).toBeTruthy();
  const response = await request.get(`${process.env.E2E_API_URL}/api/v1/assistant/sessions/${session.session_id}/artifacts`, { headers });
  const pdf = (await response.json()).artifacts.find((a: { format: string }) => a.format === "pdf");
  expect(pdf).toBeTruthy();
  const content = await request.get(`${process.env.E2E_API_URL}/api/v1/assistant/artifacts/${pdf.artifact_id}/download`, { headers });
  expect(content.ok()).toBeTruthy();
  const expected = await content.body();
  expect(expected.subarray(0, 4).toString()).toBe("%PDF");
  await openHistory(page, session.metadata.title, "R1-PDF-927");
  await page.getByRole("button", { name: /^(Artifacts|产物).*1/ }).click();
  const download = page.getByRole("button", { name: /^(Download|下载)$/ }).first();
  await expect(download).toBeVisible();
  const event = page.waitForEvent("download");
  await download.click();
  const file = await event;
  const actual = await fs.readFile((await file.path())!);
  expect(createHash("sha256").update(actual).digest("hex")).toBe(createHash("sha256").update(expected).digest("hex"));
  const preview = page.getByRole("button", { name: /^(Open in new tab|新标签页打开)$/ }).first();
  await expect(preview).toBeVisible();
  const popupEvent = page.waitForEvent("popup");
  await preview.click();
  const popup = await popupEvent;
  await expect(popup).toHaveURL(/^blob:/);
  await popup.close();
  await page.reload();
  await page.getByRole("button", { name: /^(Artifacts|产物).*1/ }).click();
  await expect(page.getByText("R1-PDF-927.pdf", { exact: true }).first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

test("controlled recovered image observes pending then unknown without any execution POST", async ({ page, request }) => {
  const headers = await buildAuthHeaders(request);
  const id = "3979c378-2057-41c8-ac75-a1ec51b8045b";
  const sessions = await request.get(`${process.env.E2E_API_URL}/api/v1/assistant/sessions?limit=200`, { headers });
  const session = (await sessions.json()).sessions.find((s: { session_id: string }) => s.session_id === id);
  expect(session).toBeTruthy();
  let reads = 0;
  let allowTerminal!: () => void;
  const terminalAllowed = new Promise<void>(resolve => { allowTerminal = resolve; });
  const executionPosts: string[] = [];
  page.on("request", r => {
    if (r.method() === "POST" && /\/api\/(v1\/agent\/images|v2\/agent\/threads)/.test(r.url())) executionPosts.push(r.url());
  });
  await page.route(`**/api/v1/assistant/sessions/${id}/history*`, async route => {
    const response = await route.fetch();
    const history = await response.json();
    const image = history.messages.find((m: { metadata?: { image_task_id?: string } }) => m.metadata?.image_task_id === "imt_d61ec6f47a444430a770");
    expect(image).toBeTruthy();
    if (++reads === 1) {
      image.metadata.image_generating = true;
      image.metadata.process_summary.status = "running";
      image.metadata.process_summary.outcome_uncertain = false;
    } else {
      await terminalAllowed;
    }
    await route.fulfill({ response, json: history });
  });
  await openHistory(page, session.metadata.title, "R1-IMAGE-PROOF-927D");
  await page.getByRole("textbox", { name: /Assistant message composer|助手消息输入框/ }).fill("Controlled image recovery draft; do not send");
  await expect(page.getByRole("button", { name: /^(Send|发送)$/ })).toBeDisabled();
  allowTerminal();
  await expect(page.getByText(/^(结果未知|Result unknown)$/).first()).toBeVisible();
  expect(reads).toBeGreaterThanOrEqual(2);
  await page.reload();
  await expect(page.getByText("imt_d61ec6f47a444430a770", { exact: false }).first()).toBeVisible();
  expect(executionPosts).toEqual([]);
});
