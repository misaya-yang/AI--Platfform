import { chromium } from "@playwright/test";

const BASE_URL = process.env.E2E_BASE_URL || "http://127.0.0.1:3000";
const EMAIL = process.env.E2E_USER_EMAIL || "admin@hejazfs.com.au";
const PASSWORD = process.env.E2E_USER_PASSWORD || "123456.dc";
const SCREENSHOT_DIR = process.env.E2E_IMAM_SCREENSHOT_DIR || "/tmp";

const scenarios = [
  {
    name: "pillars_followup",
    turns: [
      {
        prompt: "五功是什么？",
        expectAny: ["五功", "信仰", "礼拜", "天课", "斋戒", "朝觐"],
      },
      {
        prompt: "第二项请展开讲一下",
        expectAny: ["礼拜", "拜功", "Salat", "每日五次", "祈祷"],
      },
      {
        prompt: "请用更简洁的中文总结",
        expectAny: ["礼拜", "拜功", "总结", "简洁"],
      },
    ],
  },
  {
    name: "ramadan_followup",
    turns: [
      {
        prompt: "我想了解斋月相关事项",
        expectAny: ["斋戒", "斋月", "开斋", "封斋", "Sources:"],
      },
      {
        prompt: "旅行中可以不封斋吗？",
        expectAny: ["旅行", "旅途", "补斋", "宽免", "斋戒"],
      },
      {
        prompt: "如果生病呢？",
        expectAny: ["生病", "疾病", "补斋", "宽免", "斋戒"],
      },
    ],
  },
  {
    name: "context_switch_and_new_session",
    turns: [
      {
        prompt: "什么情况下可以做小净？",
        expectAny: ["小净", "净礼", "礼拜", "洗"],
      },
      {
        prompt: "需要按四大法学派比较一下",
        expectAny: ["Hanafi", "Maliki", "Shafi", "Hanbali", "四大", "School"],
      },
    ],
    newSessionAfter: true,
    isolatedTurn: {
      prompt: "第二项请展开讲一下",
      expectAny: ["没有足够信息", "请提供", "能否说明", "第二项", "咨询", "哪方面"],
    },
  },
];

function nowMs() {
  return Date.now();
}

async function loginIfNeeded(page) {
  await page.goto(`${BASE_URL}/playground`, {
    waitUntil: "networkidle",
    timeout: 60_000,
  });
  const emailInput = page.locator("#email");
  if (await emailInput.isVisible().catch(() => false)) {
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
}

async function selectImam(page) {
  const trigger = page.getByRole("combobox").first();
  await trigger.click();
  await page.getByRole("option", { name: "Imam" }).click();
}

async function ensureFreshSession(page) {
  const clearButton = page.getByRole("button", { name: /clear|清空/i });
  if (await clearButton.isVisible().catch(() => false)) {
    await clearButton.click();
  }
}

async function waitForStreamLifecycle(page) {
  const resolved = await page
    .waitForFunction(() => {
      const composer = document.querySelector("#playground-chat-composer");
      return composer instanceof HTMLTextAreaElement && !composer.disabled;
    }, undefined, { timeout: 120_000 })
    .then(() => true)
    .catch(() => false);
  return resolved;
}

async function readLastAssistantMessage(page) {
  const messages = page.locator('[data-message-role="assistant"]');
  const count = await messages.count();
  if (count === 0) {
    return { text: "", statsText: "" };
  }
  const last = messages.nth(count - 1);
  const text = (
    await last.locator('[data-message-text="true"]').first().innerText().catch(() => "")
  ).trim();
  const stats = (
    await last.locator('[data-message-stats="true"]').first().innerText().catch(() => "")
  ).trim();
  return { text, statsText: String(stats || "").trim() };
}

async function sendTurn(page, prompt, expectAny) {
  console.log(`[turn] prompt: ${prompt}`);
  const beforeAssistantCount = await page.locator('[data-message-role="assistant"]').count();
  const beforeLast = await readLastAssistantMessage(page);
  const composer = page.locator("#playground-chat-composer");
  await composer.fill(prompt);

  const sendButton = page.getByRole("button", { name: /send|发送/i });
  const submittedAt = nowMs();
  await sendButton.click();

  let firstVisibleMs = null;
  await page.waitForFunction(
    ({ expectedCount, previousText, previousStats }) => {
      const messages = document.querySelectorAll('[data-message-role="assistant"]');
      if (!messages.length) return false;
      const last = messages[messages.length - 1];
      const text = (last?.textContent || "").trim();
      if (!text.length) return false;
      if (messages.length > expectedCount) return true;
      const maybeStats = last?.nextElementSibling?.textContent || "";
      return text !== previousText || maybeStats.trim() !== previousStats;
    },
    {
      expectedCount: beforeAssistantCount,
      previousText: beforeLast.text,
      previousStats: beforeLast.statsText,
    },
    { timeout: 90_000 }
  );
  firstVisibleMs = nowMs() - submittedAt;
  console.log(`[turn] first visible content in ${firstVisibleMs}ms`);

  const streamSettled = await waitForStreamLifecycle(page);
  if (!streamSettled) {
    const debugState = await page.evaluate(() => window.__playgroundDebug ?? null).catch(() => null);
    throw new Error(
      `Composer did not re-enable within 120s for prompt "${prompt}". Debug: ${JSON.stringify(debugState)}`
    );
  }
  const completedAt = nowMs();
  const { text, statsText } = await readLastAssistantMessage(page);

  const matched = expectAny.some((needle) => text.includes(needle));
  if (!matched) {
    throw new Error(
      `Response did not include any expected markers for prompt "${prompt}". Expected one of: ${expectAny.join(", ")}\nActual: ${text.slice(0, 1200)}`
    );
  }

  console.log(`[turn] completed in ${completedAt - submittedAt}ms`);

  return {
    prompt,
    firstVisibleMs,
    totalMs: completedAt - submittedAt,
    statsText,
    excerpt: text.slice(0, 1200),
  };
}

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });

try {
  await loginIfNeeded(page);
  await selectImam(page);

  const report = [];

  for (const scenario of scenarios) {
    console.log(`[scenario] start ${scenario.name}`);
    await ensureFreshSession(page);
    const scenarioResult = { name: scenario.name, turns: [] };

    for (const turn of scenario.turns) {
      try {
        const result = await sendTurn(page, turn.prompt, turn.expectAny);
        scenarioResult.turns.push({ ok: true, ...result });
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        console.error(`[turn] failed: ${message}`);
        scenarioResult.turns.push({ ok: false, prompt: turn.prompt, error: message });
        break;
      }
    }

    if (scenario.newSessionAfter && scenario.isolatedTurn) {
      await ensureFreshSession(page);
      try {
        const isolated = await sendTurn(
          page,
          scenario.isolatedTurn.prompt,
          scenario.isolatedTurn.expectAny
        );
        scenarioResult.isolatedTurn = { ok: true, ...isolated };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        console.error(`[isolated] failed: ${message}`);
        scenarioResult.isolatedTurn = {
          ok: false,
          prompt: scenario.isolatedTurn.prompt,
          error: message,
        };
      }
    }

    await page.screenshot({
      path: `${SCREENSHOT_DIR}/imam-${scenario.name}.png`,
      fullPage: true,
    });
    console.log(`[scenario] done ${scenario.name}`);
    report.push(scenarioResult);
  }

  console.log(JSON.stringify({ baseUrl: BASE_URL, report }, null, 2));
} finally {
  await browser.close();
}
