import { chromium } from '@playwright/test';
const BASE='http://127.0.0.1:3000';
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });
async function login(){
  await page.goto(BASE + '/playground', { waitUntil: 'networkidle', timeout: 60000 });
  const email = page.locator('#email');
  if (await email.isVisible().catch(() => false)) {
    await email.fill('admin@hejazfs.com.au');
    await page.locator('#password').fill('123456.dc');
    await page.getByRole('button', { name: /sign in|log in|login|登\s*录/i }).click();
    await page.waitForURL(/\/(dashboard|assistant|playground)/, { timeout: 30000 });
    await page.goto(BASE + '/playground', { waitUntil: 'networkidle', timeout: 60000 });
  }
}
async function selectImam(){
  await page.getByRole('combobox').first().click();
  await page.getByRole('option', { name: 'Imam' }).click();
}
async function clearChat(){
  const btn = page.getByRole('button', { name: /clear|清空/i });
  if (await btn.isVisible().catch(() => false)) await btn.click();
}
async function waitComposer(){
  await page.waitForFunction(() => {
    const c = document.querySelector('#playground-chat-composer');
    return c instanceof HTMLTextAreaElement && !c.disabled;
  }, undefined, { timeout: 120000 });
}
async function dump(label){
  const data = await page.evaluate(() => Array.from(document.querySelectorAll('[data-message-role="assistant"]')).map((el, i) => ({
    index: i,
    text: el.innerText,
    msgText: el.querySelector('[data-message-text="true"]')?.textContent || '',
    toolPanels: Array.from(el.querySelectorAll('[data-tool-call-name]')).map(x => x.textContent || '')
  })));
  console.log('\n### ' + label);
  console.log(JSON.stringify(data, null, 2));
}
async function send(prompt){
  await page.locator('#playground-chat-composer').fill(prompt);
  await page.getByRole('button', { name: /send|发送/i }).click();
  await waitComposer();
  await page.waitForTimeout(1500);
  await dump(prompt);
}
await login();
await selectImam();
await clearChat();
await send('我想了解斋月相关事项');
await send('旅行中可以不封斋吗？');
await send('如果生病呢？');
await browser.close();
