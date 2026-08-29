import { expect, test, type Page } from "@playwright/test";
import { installClientAuth, seedClientPrefs } from "./support/helpers";

/**
 * Mock contract for the document lifecycle surface (C6):
 *   1. enable/disable through PATCH /documents/{id}/status, with the row
 *      badge flipping to the resolved display status;
 *   2. archive/unarchive through PATCH /documents/{id}/archive, including
 *      the optional reason (<= 255 chars client-side, dependency D2);
 *   3. the 409 queued contract: a single reindex of an already-queued
 *      document reports "already queued" instead of a raw error
 *      (PRD §5-#6), and batch-reindex reports skipped ids — partial success
 *      in the toast, an in-dialog skipped list on the all-skipped 409
 *      (PRD §5-#7/#8).
 *
 * Every route is fulfilled in-process with a mutable document fixture, so the
 * suite runs against `pnpm dev` alone and never needs the live stack. The
 * fixture list stamps enabled/archived/display_status the way the list will
 * once backend dependency D1 lands; pre-D1 the live list omits those fields.
 */

const DATASET_ID = "mock-doc-lifecycle";

interface MockDoc {
  document_id: string;
  dataset_id: string;
  title: string;
  status: string;
  display_status: string;
  enabled: boolean;
  archived: boolean;
  archived_reason?: string | null;
  word_count: number;
  char_count: number;
  size_bytes: number;
  segment_count: number;
  created_at: string;
  updated_at: string;
}

function makeDocs(): MockDoc[] {
  const base = {
    dataset_id: DATASET_ID,
    word_count: 120,
    char_count: 240,
    size_bytes: 480,
    segment_count: 3,
    created_at: "2026-08-20T09:00:00Z",
    updated_at: "2026-08-20T09:05:00Z",
  };
  return [
    {
      ...base,
      document_id: "doc-handbook",
      title: "员工手册",
      status: "completed",
      display_status: "available",
      enabled: true,
      archived: false,
    },
    {
      ...base,
      document_id: "doc-policy",
      title: "考勤制度",
      status: "completed",
      display_status: "available",
      enabled: true,
      archived: false,
    },
    {
      ...base,
      document_id: "doc-legacy",
      title: "旧版报销制度",
      // Already owns a queue generation: reindex claims must 409.
      status: "waiting",
      display_status: "queuing",
      enabled: true,
      archived: false,
    },
  ];
}

function restamp(doc: MockDoc) {
  if (doc.archived) doc.display_status = "archived";
  else if (doc.status === "completed")
    doc.display_status = doc.enabled ? "available" : "disabled";
  else if (doc.status === "waiting") doc.display_status = "queuing";
}

function jsonResponse(body: unknown, status = 200) {
  return {
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  };
}

function watchClientErrors(page: Page, allowedConsoleErrors: RegExp[] = []) {
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });
  return () => {
    expect(pageErrors, `Page errors:\n${pageErrors.join("\n")}`).toEqual([]);
    const unexpectedConsoleErrors = consoleErrors.filter(
      (message) => !allowedConsoleErrors.some((pattern) => pattern.test(message))
    );
    expect(
      unexpectedConsoleErrors,
      `Unexpected console errors:\n${unexpectedConsoleErrors.join("\n")}`
    ).toEqual([]);
  };
}

async function installLifecycleHarness(page: Page) {
  const docs = makeDocs();
  const captured = {
    statusUpdates: [] as Array<{ documentId: string; body: Record<string, unknown> }>,
    archiveUpdates: [] as Array<{ documentId: string; body: Record<string, unknown> }>,
    reindexes: [] as string[],
    batches: [] as Array<Record<string, unknown>>,
  };

  await installClientAuth(page, {
    permissions: ["console:dashboard:view", "knowledge:dataset:view"],
    effective_permissions: ["console:dashboard:view", "knowledge:dataset:view"],
  });
  await seedClientPrefs(page, { locale: "zh-CN" });

  await page.route("**/api/v1/knowledge/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const pathname = new URL(request.url()).pathname;

    if (method === "GET" && pathname === `/api/v1/knowledge/datasets/${DATASET_ID}`) {
      await route.fulfill(
        jsonResponse({
          dataset_id: DATASET_ID,
          name: "Mock Document Lifecycle",
          description: "Browser-only document lifecycle harness",
          visibility: "tenant",
          embedding_provider: "local",
          embedding_model: "hash-384",
          embedding_dimension: 384,
          statistics: { document_count: 3, segment_count: 9, token_count: 180 },
        })
      );
      return;
    }

    if (method === "GET" && pathname === `/api/v1/knowledge/${DATASET_ID}/documents`) {
      await route.fulfill(jsonResponse(docs));
      return;
    }

    // Batch endpoints must match before the per-document regexes.
    if (
      method === "POST" &&
      pathname === `/api/v1/knowledge/${DATASET_ID}/documents/batch-reindex`
    ) {
      const body = request.postDataJSON() as { document_ids?: string[] };
      captured.batches.push(body);
      const ids = body.document_ids ?? [];
      const queuedIds = ids.filter(
        (id) => docs.find((d) => d.document_id === id)?.status !== "waiting"
      );
      const skippedIds = ids.filter((id) => !queuedIds.includes(id));
      if (queuedIds.length === 0) {
        // All-skipped contract: 409 carrying the skipped ids.
        await route.fulfill(
          jsonResponse(
            {
              detail: {
                message: "No document entered a new ingestion generation",
                skipped_document_ids: skippedIds,
              },
            },
            409
          )
        );
        return;
      }
      queuedIds.forEach((id) => {
        const doc = docs.find((d) => d.document_id === id);
        if (doc) {
          doc.status = "waiting";
          restamp(doc);
        }
      });
      await route.fulfill(
        jsonResponse({
          status: skippedIds.length > 0 ? "partial" : "queuing",
          document_count: queuedIds.length,
          queued_document_ids: queuedIds,
          skipped_document_ids: skippedIds,
        })
      );
      return;
    }

    const statusMatch = pathname.match(
      /^\/api\/v1\/knowledge\/[^/]+\/documents\/([^/]+)\/status$/
    );
    if (method === "PATCH" && statusMatch) {
      const documentId = statusMatch[1];
      const body = request.postDataJSON() as { enabled?: boolean };
      captured.statusUpdates.push({ documentId, body });
      const doc = docs.find((d) => d.document_id === documentId);
      if (doc && typeof body.enabled === "boolean") {
        doc.enabled = body.enabled;
        restamp(doc);
      }
      await route.fulfill(jsonResponse(doc ?? {}));
      return;
    }

    const archiveMatch = pathname.match(
      /^\/api\/v1\/knowledge\/[^/]+\/documents\/([^/]+)\/archive$/
    );
    if (method === "PATCH" && archiveMatch) {
      const documentId = archiveMatch[1];
      const body = request.postDataJSON() as { archived?: boolean; reason?: string | null };
      captured.archiveUpdates.push({ documentId, body });
      const doc = docs.find((d) => d.document_id === documentId);
      if (doc && typeof body.archived === "boolean") {
        doc.archived = body.archived;
        doc.archived_reason = body.archived ? body.reason ?? null : null;
        restamp(doc);
      }
      await route.fulfill(jsonResponse(doc ?? {}));
      return;
    }

    const reindexMatch = pathname.match(
      /^\/api\/v1\/knowledge\/[^/]+\/documents\/([^/]+)\/reindex$/
    );
    if (method === "POST" && reindexMatch) {
      const documentId = reindexMatch[1];
      captured.reindexes.push(documentId);
      const doc = docs.find((d) => d.document_id === documentId);
      if (!doc || doc.status === "waiting") {
        await route.fulfill(
          jsonResponse(
            { detail: "Document is already queued; the durable queue owns this generation" },
            409
          )
        );
        return;
      }
      doc.status = "waiting";
      restamp(doc);
      await route.fulfill(jsonResponse({ status: "queuing", document_id: documentId }));
      return;
    }

    await route.fulfill(jsonResponse([]));
  });

  return captured;
}

async function openDocumentsTab(page: Page) {
  await page.goto(`/knowledge/${DATASET_ID}?tab=documents`);
  await expect(page.getByRole("button", { name: "员工手册" })).toBeVisible();
}

test.describe("@mock KB document lifecycle", () => {
  test("disables a document through PATCH /status and surfaces the badge", async ({
    page,
  }) => {
    const assertNoClientErrors = watchClientErrors(page);
    const captured = await installLifecycleHarness(page);
    await openDocumentsTab(page);

    const toggle = page.getByTestId("doc-switch-doc-policy");
    await expect(toggle).toBeChecked();
    await toggle.click();

    await expect.poll(() => captured.statusUpdates.length).toBeGreaterThan(0);
    expect(captured.statusUpdates[0]).toEqual({
      documentId: "doc-policy",
      body: { enabled: false },
    });

    await expect(toggle).not.toBeChecked();
    await expect(page.getByTestId("doc-disabled-badge-doc-policy")).toBeVisible();
    // exact: Radix renders an aria-live twin of every toast.
    await expect(page.getByText("文档已禁用", { exact: true })).toBeVisible();
    assertNoClientErrors();
  });

  test("archives with a reason, then unarchives through the confirm dialog", async ({
    page,
  }) => {
    const assertNoClientErrors = watchClientErrors(page);
    const captured = await installLifecycleHarness(page);
    await openDocumentsTab(page);

    await page.getByTestId("doc-archive-doc-handbook").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("归档文档");
    await dialog.getByTestId("archive-reason-input").fill("内容过期");
    await dialog.getByTestId("archive-confirm").click();

    await expect.poll(() => captured.archiveUpdates.length).toBeGreaterThan(0);
    expect(captured.archiveUpdates[0]).toEqual({
      documentId: "doc-handbook",
      body: { archived: true, reason: "内容过期" },
    });

    await expect(page.getByTestId("doc-archived-badge-doc-handbook")).toBeVisible();
    await expect(page.getByText("文档已归档", { exact: true })).toBeVisible();
    // An archived document cannot be toggled until it is restored.
    await expect(page.getByTestId("doc-switch-doc-handbook")).toBeDisabled();

    await page.getByTestId("doc-unarchive-doc-handbook").click();
    await expect(page.getByRole("alertdialog")).toContainText("恢复文档");
    await page.getByTestId("unarchive-confirm").click();

    await expect.poll(() => captured.archiveUpdates.length).toBe(2);
    expect(captured.archiveUpdates[1]).toEqual({
      documentId: "doc-handbook",
      body: { archived: false },
    });
    await expect(page.getByTestId("doc-archived-badge-doc-handbook")).toHaveCount(0);
    await expect(page.getByText("文档已恢复", { exact: true })).toBeVisible();
    assertNoClientErrors();
  });

  test("single reindex 409 is reported as already queued, not an error", async ({
    page,
  }) => {
    // The 409 is the contract under test; the browser logs the non-2xx
    // resource fetch as a console error even though the app handles it.
    const assertNoClientErrors = watchClientErrors(page, [
      /Failed to load resource.*409/,
    ]);
    const captured = await installLifecycleHarness(page);
    await openDocumentsTab(page);

    await page.getByTestId("doc-reindex-doc-legacy").click();
    await expect(page.getByRole("alertdialog")).toContainText("确认重新构建索引？");
    await page.getByRole("alertdialog").getByRole("button", { name: "确认重建" }).click();

    await expect.poll(() => captured.reindexes).toContain("doc-legacy");
    await expect(page.getByText("已在队列", { exact: true })).toBeVisible();
    // The failure path must not fire alongside.
    await expect(page.getByText("重建索引失败")).toHaveCount(0);
    assertNoClientErrors();
  });

  test("batch reindex reports queued vs skipped on partial success", async ({ page }) => {
    const assertNoClientErrors = watchClientErrors(page);
    const captured = await installLifecycleHarness(page);
    await openDocumentsTab(page);

    await page.getByRole("button", { name: /批量操作/ }).first().click();
    await page.getByRole("menuitem", { name: "进入批量模式" }).click();
    await page.getByTestId("doc-select-doc-handbook").click();
    await page.getByTestId("doc-select-doc-legacy").click();

    await page.getByRole("button", { name: /批量操作/ }).first().click();
    await page.getByRole("menuitem", { name: /^批量重建索引 \(2\)/ }).click();

    const batchRequest = page.waitForRequest((request) =>
      request.url().includes(`/api/v1/knowledge/${DATASET_ID}/documents/batch-reindex`)
    );
    await page.getByRole("button", { name: /^确认重建 \(2\)/ }).click();

    const body = (await batchRequest).postDataJSON() as Record<string, unknown>;
    expect(body.document_ids).toEqual(["doc-handbook", "doc-legacy"]);
    expect(captured.batches).toHaveLength(1);

    // Partial success names both sides of the split.
    // Anchored: the aria-live twin prefixes the toast text with "Notification".
    await expect(page.getByText(/^已入队 1 个，跳过 1 个/)).toBeVisible();
    assertNoClientErrors();
  });

  test("batch reindex 409 keeps the dialog open and lists skipped documents", async ({
    page,
  }) => {
    // The all-skipped 409 is the contract under test (see above).
    const assertNoClientErrors = watchClientErrors(page, [
      /Failed to load resource.*409/,
    ]);
    const captured = await installLifecycleHarness(page);
    await openDocumentsTab(page);

    await page.getByRole("button", { name: /批量操作/ }).first().click();
    await page.getByRole("menuitem", { name: "进入批量模式" }).click();
    await page.getByTestId("doc-select-doc-legacy").click();

    await page.getByRole("button", { name: /批量操作/ }).first().click();
    await page.getByRole("menuitem", { name: /^批量重建索引 \(1\)/ }).click();
    await page.getByRole("button", { name: /^确认重建 \(1\)/ }).click();

    await expect.poll(() => captured.batches.length).toBeGreaterThan(0);
    await expect(page.getByText("没有文档进入新的处理轮次", { exact: true })).toBeVisible();

    // The dialog stays open and shows which documents were skipped so the
    // user can deselect them and retry.
    const skipped = page.getByTestId("batch-reindex-skipped");
    await expect(skipped).toBeVisible();
    await expect(skipped).toContainText("旧版报销制度");
    assertNoClientErrors();
  });

  test("row lifecycle controls stay usable at mobile width", async ({ page }) => {
    const assertNoClientErrors = watchClientErrors(page);
    await page.setViewportSize({ width: 375, height: 812 });
    await installLifecycleHarness(page);
    await openDocumentsTab(page);

    const toggle = page.getByTestId("doc-switch-doc-handbook");
    const archive = page.getByTestId("doc-archive-doc-handbook");
    await expect(toggle).toBeVisible();
    await expect(archive).toBeVisible();

    const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(scrollWidth).toBeLessThanOrEqual(375);
    assertNoClientErrors();
  });
});
