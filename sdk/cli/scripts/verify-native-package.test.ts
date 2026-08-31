import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { afterEach, describe, expect, it } from "vitest";

const cliRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(cliRoot, "../..");
const target = "linux-arm64";
let vendorRoot = "";

afterEach(() => {
  if (vendorRoot) rmSync(vendorRoot, { recursive: true, force: true });
  vendorRoot = "";
});

describe("native package verifier", () => {
  it("accepts a source-matched local Linux arm64 receipt and rejects binary tampering", () => {
    const source = JSON.parse(readFileSync(join(repositoryRoot, "deploy/agent-runtime-source/source-receipt.json"), "utf8"));
    const overlay = JSON.parse(readFileSync(join(repositoryRoot, "rust/agent-runtime-overlay/manifest.json"), "utf8"));
    const binary = Buffer.from("synthetic-native-cli");
    vendorRoot = mkdtempSync(join(tmpdir(), "ai-gateway-native-package-test-"));
    const targetDirectory = join(vendorRoot, target);
    mkdirSync(targetDirectory, { recursive: true });
    const sourceIdentity = JSON.stringify({
      schema_version: "ai-gateway-cli/native-source/v1",
      upstream_sha: source.source.upstream_sha,
      overlay_sha256: overlay.sha256,
    });
    writeFileSync(join(vendorRoot, "source.json"), sourceIdentity);
    writeFileSync(join(vendorRoot, "expected-source.json"), sourceIdentity);
    writeFileSync(join(targetDirectory, "codex"), binary);
    writeFileSync(join(targetDirectory, "artifact.json"), JSON.stringify({
      schema_version: "ai-gateway-cli/native-artifact/v1",
      upstream_sha: source.source.upstream_sha,
      overlay_sha256: overlay.sha256,
      target_system: "linux",
      target_arch: "arm64",
      binary: "codex",
      sha256: createHash("sha256").update(binary).digest("hex"),
    }));

    expect(runVerifier().status).toBe(0);
    writeFileSync(join(targetDirectory, "codex"), "tampered");
    const tampered = runVerifier();
    expect(tampered.status).toBe(1);
    expect(tampered.stderr).toContain("binary SHA-256 mismatch");
  });
});

function runVerifier() {
  return spawnSync(process.execPath, [join(cliRoot, "scripts", "verify-native-package.mjs")], {
    cwd: cliRoot,
    encoding: "utf8",
    env: {
      ...process.env,
      AI_GATEWAY_CLI_PACKAGE_TARGETS: target,
      AI_GATEWAY_CLI_VENDOR_ROOT: vendorRoot,
      AI_GATEWAY_CLI_DIST_SOURCE: join(vendorRoot, "expected-source.json"),
    },
  });
}
