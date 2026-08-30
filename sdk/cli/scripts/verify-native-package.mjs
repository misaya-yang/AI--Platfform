import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const SCHEMA = "ai-gateway-cli/native-artifact/v1";
const SOURCE_SCHEMA = "ai-gateway-cli/native-source/v1";
const cliRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(cliRoot, "../..");
const sourceReceipt = readJson(resolve(repositoryRoot, "deploy/agent-runtime-source/source-receipt.json"));
const overlayManifest = readJson(resolve(repositoryRoot, "rust/agent-runtime-overlay/manifest.json"));
const sourceLock = readJson(resolve(repositoryRoot, "deploy/agent-runtime-source/lock.json"));
const UPSTREAM_SHA = sourceReceipt.source?.upstream_sha;
const OVERLAY_SHA = overlayManifest.sha256;
if (sourceReceipt.overlay?.sha256 !== OVERLAY_SHA || sourceLock.build?.overlay_sha256 !== OVERLAY_SHA) {
  fail("Agent Runtime source receipt, overlay manifest, and lock disagree");
}
if (overlayManifest.upstream_sha !== UPSTREAM_SHA || sourceLock.source?.upstream_sha !== UPSTREAM_SHA) {
  fail("Agent Runtime upstream identity disagrees across source records");
}
const defaultTarget = `${process.platform}-${process.arch}`;
const vendorRoot = resolve(process.env.AI_GATEWAY_CLI_VENDOR_ROOT || resolve(cliRoot, "vendor"));
const packageSource = readJson(resolve(vendorRoot, "source.json"));
const distSource = readJson(resolve(process.env.AI_GATEWAY_CLI_DIST_SOURCE || resolve(cliRoot, "dist", "native-source.json")));
if (packageSource.schema_version !== SOURCE_SCHEMA || packageSource.upstream_sha !== UPSTREAM_SHA || packageSource.overlay_sha256 !== OVERLAY_SHA) {
  fail("Packaged native source identity does not match the repository source records");
}
if (distSource.schema_version !== SOURCE_SCHEMA || distSource.upstream_sha !== UPSTREAM_SHA || distSource.overlay_sha256 !== OVERLAY_SHA) {
  fail("Built launcher source identity does not match the repository source records");
}
const targets = (process.env.AI_GATEWAY_CLI_PACKAGE_TARGETS || defaultTarget)
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean);

if (!targets.length) fail("AI_GATEWAY_CLI_PACKAGE_TARGETS selected no targets");
for (const target of targets) verifyTarget(target);
console.log(`Verified ${targets.length} native CLI package target(s): ${targets.join(", ")}`);

function verifyTarget(target) {
  const [platform, arch] = target.split("-");
  if (!platform || !arch) fail(`Invalid native package target: ${target}`);
  const binaryName = platform === "win32" ? "codex.exe" : "codex";
  const directory = resolve(vendorRoot, target);
  const binary = resolve(directory, binaryName);
  const receiptPath = resolve(directory, "artifact.json");
  if (!existsSync(binary) || !existsSync(receiptPath)) {
    fail(`Missing native binary or receipt for ${target}`);
  }
  const receipt = JSON.parse(readFileSync(receiptPath, "utf8"));
  const targetSystem = platform === "win32" ? "windows" : platform;
  const expected = {
    schema_version: SCHEMA,
    upstream_sha: UPSTREAM_SHA,
    overlay_sha256: OVERLAY_SHA,
    target_system: targetSystem,
    target_arch: arch,
    binary: binaryName,
  };
  for (const [key, value] of Object.entries(expected)) {
    if (receipt[key] !== value) fail(`Native receipt ${key} mismatch for ${target}`);
  }
  const digest = createHash("sha256").update(readFileSync(binary)).digest("hex");
  if (receipt.sha256 !== digest) fail(`Native binary SHA-256 mismatch for ${target}`);
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    fail(`Cannot read source identity ${path}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function fail(message) {
  console.error(`ERROR: ${message}`);
  process.exit(1);
}
