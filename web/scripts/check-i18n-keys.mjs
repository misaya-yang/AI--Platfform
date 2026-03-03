import { readFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

function flatten(obj, prefix = "", out = new Set()) {
  if (obj && typeof obj === "object" && !Array.isArray(obj)) {
    for (const [key, value] of Object.entries(obj)) {
      const next = prefix ? `${prefix}.${key}` : key;
      flatten(value, next, out);
    }
    return out;
  }
  if (prefix) out.add(prefix);
  return out;
}

function difference(source, target) {
  const missing = [];
  for (const key of source) {
    if (!target.has(key)) missing.push(key);
  }
  return missing.sort();
}

async function readJson(filePath) {
  const raw = await readFile(filePath, "utf8");
  return JSON.parse(raw);
}

async function main() {
  const baseDir = path.resolve("src/i18n/locales");
  const [en, zh] = await Promise.all([
    readJson(path.join(baseDir, "en-US.json")),
    readJson(path.join(baseDir, "zh-CN.json")),
  ]);

  const enKeys = flatten(en);
  const zhKeys = flatten(zh);

  const missingInZh = difference(enKeys, zhKeys);
  const missingInEn = difference(zhKeys, enKeys);

  if (missingInZh.length === 0 && missingInEn.length === 0) {
    console.log("i18n check passed: en-US and zh-CN keys are in sync.");
    return;
  }

  if (missingInZh.length > 0) {
    console.error("\nMissing in zh-CN:");
    for (const key of missingInZh) console.error(`  - ${key}`);
  }

  if (missingInEn.length > 0) {
    console.error("\nMissing in en-US:");
    for (const key of missingInEn) console.error(`  - ${key}`);
  }

  process.exitCode = 1;
}

main().catch((error) => {
  console.error("Failed to check i18n keys:", error);
  process.exit(1);
});

