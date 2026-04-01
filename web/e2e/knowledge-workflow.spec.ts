import { expect, test } from "@playwright/test";
import { buildAuthHeaders, ensureAuthenticatedPage, getApiUrl } from "./support/helpers";

const DATASET_PREFIX = "e2e-kb-test";

function uniqueName(): string {
  return `${DATASET_PREFIX}-${Date.now()}`;
}

test.describe("Knowledge Base workflow", () => {
  test.setTimeout(3 * 60_000);

  let datasetId: string | undefined;
  let datasetName: string;
  let headers: Record<string, string>;

  test.beforeAll(async ({ request }) => {
    headers = await buildAuthHeaders(request);
  });

  test.afterAll(async ({ request }) => {
    // Clean up: delete dataset if it was created
    if (datasetId) {
      const apiUrl = getApiUrl();
      await request.delete(`${apiUrl}/api/v1/knowledge/datasets/${datasetId}`, { headers }).catch(() => {});
    }
  });

  test("full KB lifecycle: create, upload, verify, toggle, delete", async ({ page, request }) => {
    const apiUrl = getApiUrl();
    datasetName = uniqueName();

    // --- Step 1: Create dataset via API ---
    const createRes = await request.post(`${apiUrl}/api/v1/knowledge/datasets`, {
      headers,
      data: { name: datasetName, description: "E2E test dataset" },
    });
    expect(createRes.ok(), `Create dataset failed: ${createRes.status()}`).toBeTruthy();
    const created = await createRes.json();
    datasetId = created.id ?? created.dataset_id ?? created.data?.id;
    expect(datasetId).toBeTruthy();

    // --- Step 2: Navigate to KB list and verify dataset appears ---
    await ensureAuthenticatedPage(page, "/knowledge");
    const mainContent = page.locator(".ant-layout-content, main").first();
    await expect(mainContent).toBeVisible();
    await expect(page.getByText(datasetName)).toBeVisible({ timeout: 15_000 });

    // --- Step 3: Upload a text document via API ---
    const docRes = await request.post(`${apiUrl}/api/v1/knowledge/${datasetId}/documents/text`, {
      headers,
      data: {
        title: "E2E Test Document",
        text: "This is a test document created by the Playwright E2E suite. Bismillah.",
      },
    });
    expect(docRes.ok(), `Create document failed: ${docRes.status()}`).toBeTruthy();

    // --- Step 4: Verify document appears in API ---
    const docsListRes = await request.get(`${apiUrl}/api/v1/knowledge/${datasetId}/documents`, { headers });
    expect(docsListRes.ok()).toBeTruthy();
    const docsList = await docsListRes.json();
    const docs = Array.isArray(docsList) ? docsList : docsList.data ?? docsList.documents ?? [];
    expect(docs.length).toBeGreaterThanOrEqual(1);

    // --- Step 5: Navigate to dataset detail page ---
    await page.goto(`/knowledge/${datasetId}`, { waitUntil: "domcontentloaded" });
    await expect(mainContent).toBeVisible();
    // Verify document title or some content is shown
    await expect(page.getByText("E2E Test Document")).toBeVisible({ timeout: 15_000 });

    // --- Step 6: Disable dataset via API ---
    const disableRes = await request.patch(`${apiUrl}/api/v1/knowledge/datasets/${datasetId}/status`, {
      headers,
      data: { enabled: false },
    });
    if (!disableRes.ok()) {
      // Try compat route
      const disableCompat = await request.post(`${apiUrl}/api/v1/knowledge/datasets/${datasetId}/enable`, {
        headers,
        data: { enabled: false },
      });
      expect(disableCompat.ok(), "Disable dataset failed on both routes").toBeTruthy();
    }

    // Verify disabled state
    const getAfterDisable = await request.get(`${apiUrl}/api/v1/knowledge/datasets/${datasetId}`, { headers });
    expect(getAfterDisable.ok()).toBeTruthy();
    const dsDisabled = await getAfterDisable.json();
    const disabledData = dsDisabled.data ?? dsDisabled;
    expect(disabledData.enabled).toBe(false);

    // --- Step 7: Re-enable dataset via API ---
    const enableRes = await request.patch(`${apiUrl}/api/v1/knowledge/datasets/${datasetId}/status`, {
      headers,
      data: { enabled: true },
    });
    if (!enableRes.ok()) {
      const enableCompat = await request.post(`${apiUrl}/api/v1/knowledge/datasets/${datasetId}/enable`, {
        headers,
        data: { enabled: true },
      });
      expect(enableCompat.ok(), "Enable dataset failed on both routes").toBeTruthy();
    }

    // --- Step 8: Delete dataset via API ---
    const deleteRes = await request.delete(`${apiUrl}/api/v1/knowledge/datasets/${datasetId}`, { headers });
    expect(deleteRes.ok(), `Delete dataset failed: ${deleteRes.status()}`).toBeTruthy();
    datasetId = undefined; // prevent afterAll cleanup

    // --- Step 9: Verify dataset no longer in list ---
    await page.goto("/knowledge", { waitUntil: "domcontentloaded" });
    await expect(mainContent).toBeVisible();
    // Wait briefly for list to load, then confirm dataset is gone
    await page.waitForTimeout(2_000);
    await expect(page.getByText(datasetName)).toHaveCount(0);
  });
});
