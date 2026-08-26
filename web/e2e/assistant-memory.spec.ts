/**
 * Assistant memory persistence E2E.
 *
 * Memory writes go through `update_user_memory`, a write capability. The Agent
 * Runtime starts every thread with `approvalPolicy: on-request` and a
 * read-only sandbox, so each write parks on an operator approval before it can
 * run — and the model may request one write per remembered fact. That makes
 * this an inherently UI-driven flow: an API client has no way to grant the
 * approvals, so it would simply hang on the open events cursor.
 */

import { expect, test, type Page } from "@playwright/test";
import { ensureAuthenticatedPage } from "./support/helpers";

const COMPOSER = "#assistant-chat-composer";
const APPROVE = /^(Approve|通过)$/;
const STOP = /^(Stop generating|停止生成)$/;

/** Send one turn, granting every approval it parks on, and wait for it to settle. */
async function sendGrantingApprovals(
  page: Page,
  message: string,
  { budgetMs = 150_000, maxApprovals = 8 } = {},
): Promise<number> {
  const composer = page.locator(COMPOSER);
  await expect(composer).toBeVisible();
  await composer.fill(message);
  await composer.press("Enter");

  const approve = page.getByRole("button", { name: APPROVE });
  const stop = page.getByRole("button", { name: STOP });
  const deadline = Date.now() + budgetMs;
  let granted = 0;

  while (Date.now() < deadline) {
    if (granted < maxApprovals && (await approve.count()) > 0) {
      await approve.first().click();
      granted += 1;
      await page.waitForTimeout(1_500);
      continue;
    }
    if (!(await stop.isVisible().catch(() => false))) break;
    await page.waitForTimeout(1_000);
  }

  await expect(stop).toBeHidden({ timeout: 30_000 });
  return granted;
}

async function startFreshChat(page: Page) {
  const newChat = page.getByRole("button", { name: /^(New chat|新对话)$/ });
  if ((await newChat.count()) === 0) {
    await page.getByRole("button", { name: /^(Show history|显示历史)$/ }).first().click();
  }
  await newChat.first().click();
  await expect(page.locator(COMPOSER)).toBeVisible();
}

test("assistant retains user identity across fresh sessions", async ({ page }) => {
  test.setTimeout(6 * 60_000);

  // Digits, not base36. A mixed alphanumeric token like "mt9isagu" is exactly
  // the kind of string a model mis-transcribes into the tool call (observed:
  // it stored "...mt9isagi"), which fails this test for a reason that has
  // nothing to do with whether memory persisted.
  const uniqueSuffix = String(Date.now() % 1_000_000).padStart(6, "0");
  const preferredName = `测试用户-${uniqueSuffix}`;

  await ensureAuthenticatedPage(page, "/assistant");

  // One fact, and a semantic one. The turn runs under the default `basic`
  // memory profile, which the worker gates to `memory_type: "semantic"`; a
  // fact the model chooses to tag `situational` (a home city, say) is denied
  // by policy, so asserting on it would make this test depend on the
  // taxonomy the model happens to pick.
  // Name the capability. "请记住…只回复“已记住”" reads to the model as
  // *answer briefly and do nothing*, and it sometimes obliges: the write turn
  // then succeeds having stored nothing, and the failure surfaces one turn
  // later as a recall miss that looks like broken persistence.
  const granted = await sendGrantingApprovals(
    page,
    `请调用 update_user_memory，把“我的名字是${preferredName}”写入长期记忆。`
      + `写入完成后只回复“已记住”。`,
  );
  // `update_user_memory` is a write capability, so a real write always parks on
  // an approval first. Zero approvals means the model answered without calling
  // it — fail here, where the cause is legible, not on the recall assertion.
  expect(granted, "model did not request the update_user_memory approval").toBeGreaterThan(0);

  const log = page.locator('[role="log"]');
  await expect(log).not.toContainText(/Approval required|需要审批/);

  await startFreshChat(page);
  // Ask for a verbatim echo: left to itself the model answers with a tidied-up
  // paraphrase ("你的名字是测试用户"), which says nothing about whether the
  // run-unique suffix survived the round trip through memory.
  await sendGrantingApprovals(
    page,
    "你还记得我的名字吗？请逐字重复我告诉过你的名字，包含其中的所有字符，不要省略或改写。",
  );

  const recallText = await log.innerText();
  expect(recallText).toContain(preferredName);
});
