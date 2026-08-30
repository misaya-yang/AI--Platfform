import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { delimiter, dirname, join, resolve } from "node:path";

import {
  LOCAL_PROXY_TOKEN_ENV,
  startChatCompatibilityProxy,
  type ChatCompatibilityProxy,
} from "../provider/chat_responses_proxy.js";
import {
  PRODUCT_CONFIG_SCHEMA,
  assertProviderCredentials,
  defaultProductConfig,
  ensurePrivateDirectory,
  loadProductConfig,
  productConfigPath,
  productHome,
  runtimeHome,
  saveProductConfig,
  validateProductConfig,
  writeRuntimeConfig,
  type ProductConfig,
  type ProviderProfile,
} from "../provider/config.js";
import { assertNativeSourceMatch, readNativeSourceIdentity, verifyNativeArtifact } from "./identity.js";

const NATIVE_CHILD_ENV_ALLOWLIST = [
  "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "TMP", "TEMP",
  "LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM", "NO_COLOR", "FORCE_COLOR",
  "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
  "SSH_AUTH_SOCK", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
] as const;
const PROVIDER_ADD_FLAGS = new Set([
  "name", "model", "base-url", "wire-api", "api-key-env", "auth-header",
  "allow-insecure-localhost",
]);

interface LauncherDependencies {
  spawnProcess?: typeof spawn;
  startProxy?: typeof startChatCompatibilityProxy;
  stdout?: Pick<NodeJS.WriteStream, "write">;
  stderr?: Pick<NodeJS.WriteStream, "write">;
  platform?: NodeJS.Platform;
  arch?: string;
  moduleUrl?: string;
  hasExecutable?: (name: string, env: NodeJS.ProcessEnv) => boolean;
  signalSource?: Pick<NodeJS.Process, "on" | "off">;
}

export async function runIndependentCli(
  argv: string[],
  env: NodeJS.ProcessEnv = process.env,
  dependencies: LauncherDependencies = {},
): Promise<number> {
  const stdout = dependencies.stdout ?? process.stdout;
  const stderr = dependencies.stderr ?? process.stderr;
  const home = productHome(env);
  const configPath = productConfigPath(home);
  if (argv[0] === "config") return handleConfigCommand(argv.slice(1), home, configPath, stdout, stderr);
  if (argv[0] === "provider") return handleProviderCommand(argv.slice(1), configPath, stdout, stderr);

  let config: ProductConfig;
  try {
    config = loadProductConfig(configPath);
  } catch (error) {
    stderr.write(`${message(error)}\nRun \`ai-gateway config init\`, edit ${configPath}, then select a provider.\n`);
    return 2;
  }
  let providerId: string;
  let forwardedArgs: string[];
  let provider: ProviderProfile;
  try {
    ({ providerId, forwardedArgs } = consumeProviderOverride(argv, config));
    const selected = config.providers[providerId];
    if (!selected) throw new Error(`Provider does not exist: ${providerId}`);
    provider = selected;
    assertProviderCredentials(provider, env);
  } catch (error) {
    stderr.write(`${message(error)}\n`);
    return 2;
  }

  const runtimeDir = runtimeHome(home);
  let proxy: ChatCompatibilityProxy | undefined;
  const childEnv = nativeChildEnvironment(env, runtimeDir);
  try {
    const platform = dependencies.platform ?? process.platform;
    if (
      platform === "linux" &&
      forwardedArgs.includes("--approve-for-me") &&
      !(dependencies.hasExecutable ?? executableOnPath)("bwrap", childEnv)
    ) {
      throw new Error(
        "--approve-for-me on Linux requires bubblewrap (bwrap); install it or use an explicit sandbox/approval mode.",
      );
    }
    if (provider.wire_api === "chat_completions") {
      proxy = await (dependencies.startProxy ?? startChatCompatibilityProxy)(provider, env);
      childEnv[LOCAL_PROXY_TOKEN_ENV] = proxy.token;
      writeRuntimeConfig(config, providerId, provider, {
        baseUrl: proxy.baseUrl,
        envKey: LOCAL_PROXY_TOKEN_ENV,
      }, runtimeDir);
    } else {
      for (const name of providerEnvironmentNames(provider)) childEnv[name] = env[name];
      writeRuntimeConfig(config, providerId, provider, undefined, runtimeDir);
    }
    const binary = resolveNativeBinary(env, {
      platform: dependencies.platform,
      arch: dependencies.arch,
      moduleUrl: dependencies.moduleUrl,
    });
    return await spawnAndWait(
      dependencies.spawnProcess ?? spawn,
      binary,
      forwardedArgs,
      childEnv,
      dependencies.signalSource ?? process,
    );
  } catch (error) {
    stderr.write(`Independent Agent CLI failed: ${message(error)}\n`);
    return 1;
  } finally {
    await proxy?.close().catch(() => undefined);
  }
}

function handleConfigCommand(
  argv: string[],
  home: string,
  path: string,
  stdout: Pick<NodeJS.WriteStream, "write">,
  stderr: Pick<NodeJS.WriteStream, "write">,
): number {
  const command = argv[0] ?? "path";
  if (command === "path") {
    stdout.write(`${path}\n`);
    return 0;
  }
  if (command === "init") {
    if (existsSync(path)) {
      stderr.write(`Config already exists: ${path}\n`);
      return 2;
    }
    ensurePrivateDirectory(home);
    saveProductConfig(defaultProductConfig(), path);
    stdout.write(`Created ${path}\nAdd a provider, then run: ai-gateway provider use <id>\n`);
    return 0;
  }
  if (command === "validate") {
    try {
      const config = loadProductConfig(path);
      const providerCount = Object.keys(config.providers).length;
      stdout.write(`Valid ${PRODUCT_CONFIG_SCHEMA} config (${providerCount} provider${providerCount === 1 ? "" : "s"})\n`);
      return 0;
    } catch (error) {
      stderr.write(`${message(error)}\n`);
      return 2;
    }
  }
  stderr.write("Usage: ai-gateway config init|path|validate\n");
  return 2;
}

function handleProviderCommand(
  argv: string[],
  path: string,
  stdout: Pick<NodeJS.WriteStream, "write">,
  stderr: Pick<NodeJS.WriteStream, "write">,
): number {
  let config: ProductConfig;
  try {
    config = loadProductConfig(path);
  } catch (error) {
    stderr.write(`${message(error)}\n`);
    return 2;
  }
  const command = argv[0] ?? "list";
  if (command === "list") {
    for (const [id, provider] of Object.entries(config.providers)) {
      stdout.write(`${id === config.active_provider ? "*" : " "} ${id}\t${provider.wire_api}\t${provider.model}\t${provider.base_url}\n`);
    }
    if (!Object.keys(config.providers).length) stdout.write("No providers configured. Edit the providers config shown by `ai-gateway config path`.\n");
    return 0;
  }
  if (command === "use") {
    const id = argv[1];
    if (!id || !config.providers[id]) {
      stderr.write(`Unknown provider: ${id ?? ""}\n`);
      return 2;
    }
    config.active_provider = id;
    saveProductConfig(config, path);
    stdout.write(`Active provider: ${id}\n`);
    return 0;
  }
  if (command === "add") {
    try {
      const { id, profile } = parseProviderAdd(argv.slice(1));
      config.providers[id] = profile;
      if (!config.active_provider) config.active_provider = id;
      saveProductConfig(validateProductConfig(config), path);
      stdout.write(`Configured provider ${id}; secret value remains in ${profile.auth.type === "none" ? "no environment variable" : profile.auth.api_key_env}.\n`);
      return 0;
    } catch (error) {
      stderr.write(`${message(error)}\n${providerAddUsage()}`);
      return 2;
    }
  }
  stderr.write(`Usage: ai-gateway provider list|use <id>|add ...\n${providerAddUsage()}`);
  return 2;
}

function parseProviderAdd(argv: string[]): { id: string; profile: ProviderProfile } {
  const id = argv[0];
  if (!id) throw new Error("provider id is required");
  const flags = new Map<string, string>();
  for (let index = 1; index < argv.length; index += 2) {
    const name = argv[index];
    const value = argv[index + 1];
    if (!name?.startsWith("--") || value === undefined) throw new Error(`invalid provider flag: ${name ?? ""}`);
    const key = name.slice(2);
    if (!PROVIDER_ADD_FLAGS.has(key)) throw new Error(`unknown provider flag: ${name}`);
    if (flags.has(key)) throw new Error(`duplicate provider flag: ${name}`);
    flags.set(key, value);
  }
  const wire = flags.get("wire-api") === "chat-completions" ? "chat_completions" : flags.get("wire-api");
  const authHeader = flags.get("auth-header");
  const apiKeyEnv = flags.get("api-key-env");
  const raw = {
    schema_version: PRODUCT_CONFIG_SCHEMA,
    active_provider: id,
    providers: {
      [id]: {
        name: flags.get("name") ?? id,
        model: flags.get("model"),
        base_url: flags.get("base-url"),
        wire_api: wire,
        auth: apiKeyEnv
          ? authHeader
            ? { type: "header", header: authHeader, api_key_env: apiKeyEnv }
            : { type: "bearer", api_key_env: apiKeyEnv }
          : { type: "none" },
        allow_insecure_localhost: flags.get("allow-insecure-localhost") === "true",
      },
    },
  };
  const validated = validateProductConfig(raw);
  return { id, profile: validated.providers[id]! };
}

function providerAddUsage(): string {
  return "  ai-gateway provider add <id> --base-url <https://.../v1> --model <id> --wire-api responses|chat-completions [--api-key-env NAME] [--auth-header NAME]\n";
}

function consumeProviderOverride(argv: string[], config: ProductConfig): { providerId: string; forwardedArgs: string[] } {
  const index = argv.indexOf("--cli-provider");
  if (index < 0) return { providerId: config.active_provider, forwardedArgs: argv };
  const providerId = argv[index + 1];
  if (!providerId) throw new Error("--cli-provider requires a provider id");
  return {
    providerId,
    forwardedArgs: [...argv.slice(0, index), ...argv.slice(index + 2)],
  };
}

function providerEnvironmentNames(provider: ProviderProfile): Set<string> {
  return new Set([
    ...(provider.auth.type === "none" ? [] : [provider.auth.api_key_env]),
    ...Object.values(provider.env_http_headers ?? {}),
  ]);
}

function executableOnPath(name: string, env: NodeJS.ProcessEnv): boolean {
  return (env.PATH ?? "")
    .split(delimiter)
    .filter(Boolean)
    .some((directory) => existsSync(join(directory, name)));
}

function nativeChildEnvironment(source: NodeJS.ProcessEnv, codexHome: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { CODEX_HOME: codexHome };
  for (const name of NATIVE_CHILD_ENV_ALLOWLIST) {
    if (source[name] !== undefined) env[name] = source[name];
  }
  return env;
}

export function resolveNativeBinary(
  env: NodeJS.ProcessEnv,
  options: { platform?: NodeJS.Platform; arch?: string; moduleUrl?: string } = {},
): string {
  const platform = options.platform ?? process.platform;
  const arch = options.arch ?? process.arch;
  const explicit = env.AI_GATEWAY_AGENT_RUNTIME_BIN?.trim();
  if (explicit) {
    const path = resolve(explicit);
    if (!existsSync(path)) throw new Error(`AI_GATEWAY_AGENT_RUNTIME_BIN does not exist: ${path}`);
    if (env.AI_GATEWAY_UNSAFE_DEV_RUNTIME !== "1") {
      throw new Error("Explicit native Runtime is development-only and requires AI_GATEWAY_UNSAFE_DEV_RUNTIME=1");
    }
    const receipt = env.AI_GATEWAY_AGENT_RUNTIME_RECEIPT?.trim();
    if (receipt) verifyNativeArtifact(path, resolve(receipt), platform, arch);
    return path;
  }
  const extension = platform === "win32" ? ".exe" : "";
  const currentModule = options.moduleUrl ?? import.meta.url;
  const packageRoot = resolve(dirname(fileURLToPath(currentModule)), "..");
  const packaged = join(packageRoot, "vendor", `${platform}-${arch}`, `codex${extension}`);
  if (existsSync(packaged)) {
    const expectedSource = readNativeSourceIdentity(join(packageRoot, "dist", "native-source.json"));
    const vendorSource = readNativeSourceIdentity(join(packageRoot, "vendor", "source.json"));
    assertNativeSourceMatch(expectedSource, vendorSource);
    verifyNativeArtifact(packaged, join(dirname(packaged), "artifact.json"), platform, arch, expectedSource);
    return packaged;
  }
  if (env.AI_GATEWAY_USE_SYSTEM_CODEX === "1" && env.AI_GATEWAY_UNSAFE_DEV_RUNTIME === "1") {
    return `codex${extension}`;
  }
  throw new Error(
    `No packaged Agent Runtime binary for ${platform}-${arch}. ` +
    "Set AI_GATEWAY_AGENT_RUNTIME_BIN to the composed-source codex binary or explicitly opt into AI_GATEWAY_USE_SYSTEM_CODEX=1.",
  );
}

function spawnAndWait(
  spawnProcess: typeof spawn,
  binary: string,
  argv: string[],
  env: NodeJS.ProcessEnv,
  signalSource: Pick<NodeJS.Process, "on" | "off">,
): Promise<number> {
  return new Promise((resolvePromise, reject) => {
    const child = spawnProcess(binary, argv, {
      stdio: "inherit",
      env,
      windowsHide: false,
    });
    const forwardSignal = (signal: NodeJS.Signals) => {
      if (child.exitCode === null && child.signalCode === null) child.kill(signal);
    };
    const onSigint = () => forwardSignal("SIGINT");
    const onSigterm = () => forwardSignal("SIGTERM");
    const cleanup = () => {
      signalSource.off("SIGINT", onSigint);
      signalSource.off("SIGTERM", onSigterm);
    };
    signalSource.on("SIGINT", onSigint);
    signalSource.on("SIGTERM", onSigterm);
    child.once("error", (error) => {
      cleanup();
      reject(error);
    });
    child.once("exit", (code, signal) => {
      cleanup();
      if (typeof code === "number") resolvePromise(code);
      else resolvePromise(signal ? 128 + signalNumber(signal) : 1);
    });
  });
}

function signalNumber(signal: NodeJS.Signals): number {
  return signal === "SIGINT" ? 2 : signal === "SIGTERM" ? 15 : 1;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
