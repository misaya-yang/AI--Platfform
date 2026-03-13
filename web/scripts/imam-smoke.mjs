import { chromium } from "@playwright/test";

const BASE_URL = process.env.E2E_BASE_URL || "http://127.0.0.1:3000";
const EMAIL = process.env.E2E_USER_EMAIL || "admin@hejazfs.com.au";
const PASSWORD = process.env.E2E_USER_PASSWORD || "123456.dc";
const PROMPT = process.env.E2E_IMAM_PROMPT || "我想了解斋月相关事项";
const SCREENSHOT =
  process.env.E2E_IMAM_SCREENSHOT ||
  "/tmp/playwright-imam-smoke.png";

async function waitForAssistantResponse(page) {
  const expected = [
    "斋戒的义务与定义",
    "关于斋月的主要事项如下",
    "Sources:",
    "All information provided is sourced",
  ];

  await page.waitForFunction(
    (needles) => {
      const bodyText = document.body.innerText || "";
      return needles.some((needle) => bodyText.includes(needle));
    },
    expected,
    { timeout: 120_000 }
  );
}

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1600, height: 1100 } });

try {
  console.log(`[smoke] opening ${BASE_URL}/playground`);
  await page.goto(`${BASE_URL}/playground`, {
    waitUntil: "networkidle",
    timeout: 60_000,
  });

  const emailInput = page.locator("#email");
  if (await emailInput.isVisible().catch(() => false)) {
    console.log("[smoke] login form detected");
    await emailInput.fill(EMAIL);
    await page.locator("#password").fill(PASSWORD);
    await page
      .getByRole("button", { name: /sign in|log in|login|登\s*录/i })
      .click();
    await page.waitForURL(/\/(dashboard|assistant|playground)/, {
      timeout: 30_000,
    });
    await page.goto(`${BASE_URL}/playground`, {
      waitUntil: "networkidle",
      timeout: 60_000,
    });
  }

  const serviceTrigger = page.getByRole("combobox").first();
  await serviceTrigger.click();
  await page.getByRole("option", { name: "Imam" }).click();
  console.log("[smoke] selected Imam service");

  const composer = page.locator("#playground-chat-composer");
  await composer.fill(PROMPT);
  await page.getByRole("button", { name: /send|发送/i }).click();
  console.log(`[smoke] submitted prompt: ${PROMPT}`);

  await waitForAssistantResponse(page);
  await page.screenshot({ path: SCREENSHOT, fullPage: true });

  const bodyText = await page.locator("body").innerText();
  const lines = bodyText
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const markerIndex = lines.findIndex((line) =>
    [
      "关于斋月的主要事项如下",
      "斋戒的义务与定义",
      "Sources:",
      "All information provided is sourced",
    ].some((needle) => line.includes(needle))
  );
  const excerpt =
    markerIndex >= 0
      ? lines.slice(Math.max(0, markerIndex - 6), markerIndex + 18)
      : lines.slice(0, 40);

  console.log("[smoke] response markers found");
  console.log("[smoke] screenshot:", SCREENSHOT);
  console.log("[smoke] response excerpt:");
  console.log(excerpt.join("\n"));
} finally {
  await browser.close();
}
