import { chromium } from '@playwright/test';
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });
const BASE='http://127.0.0.1:3000';
async function login(){
  await page.goto(BASE+'/playground',{waitUntil:'networkidle',timeout:60000});
  const email = page.locator('#email');
  if (await email.isVisible().catch(()=>false)) {
    await email.fill('admin@hejazfs.com.au');
    await page.locator('#password').fill('123456.dc');
    await page.getByRole('button', { name: /sign in|log in|login|登\s*录/i }).click();
    await page.waitForURL(/\/(dashboard|assistant|playground)/,{timeout:30000});
    await page.goto(BASE+'/playground',{waitUntil:'networkidle',timeout:60000});
  }
}
async function selectImam(){
  await page.getByRole('combobox').first().click();
  await page.getByRole('option', { name: 'Imam' }).click();
}
async function fresh(){
  const clear = page.getByRole('button', { name: /clear|清空/i });
  if (await clear.isVisible().catch(()=>false)) await clear.click();
}
async function turn(prompt){
  await page.locator('#playground-chat-composer').fill(prompt);
  await page.getByRole('button', { name: /send|发送/i }).click();
  const stop = page.getByRole('button', { name: /stop generating/i });
  await stop.waitFor({ state:'visible', timeout:15000 }).catch(()=>null);
  await stop.waitFor({ state:'hidden', timeout:120000 }).catch(()=>null);
  await page.waitForTimeout(1000);
  const texts = await page.locator('[data-message-role="assistant"]').evaluateAll((els)=>
    els.map((el, i)=>({i, text:(el.textContent||'').trim().slice(0,1500)}))
  );
  console.log('\nPROMPT', prompt);
  console.log(JSON.stringify(texts,null,2));
}
await login();
await selectImam();
await fresh();
for (const prompt of ['五功是什么？','第二项请展开讲一下','请用更简洁的中文总结']) {
  await turn(prompt);
}
await browser.close();
