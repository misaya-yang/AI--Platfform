import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium, type FullConfig } from "@playwright/test";

const AUTH_STORAGE_KEY = "agent-gateway-auth";
const APP_STORAGE_KEY = "agent-gateway-storage";
const MODEL_TESTER_PASSWORD =
  process.env.E2E_MODEL_TESTER_PASSWORD || "ModelTester-ChangeMe-2026!";
const setupDir = path.dirname(fileURLToPath(import.meta.url));
const ARTIFACT_DIR = path.resolve(setupDir, "../.playwright");
const USER_FILE = path.join(ARTIFACT_DIR, "e2e-user.json");
const REQUIRED_PATHS = [
  "/api/v1/auth/login",
  "/api/v1/auth/me",
  "/api/v1/assistant/chat/stream",
  "/api/v1/sessions",
];

function repoRoot(): string {
  return path.resolve(setupDir, "../..");
}

async function verifyApi(apiURL: string) {
  // Try both /health and /api/v1/health (production nginx proxies /api/v1/)
  const healthResponse = await fetch(`${apiURL}/api/v1/health`).catch(() =>
    fetch(`${apiURL}/health`)
  );

  if (!healthResponse.ok) {
    throw new Error(`Health check failed (${healthResponse.status})`);
  }

  // OpenAPI availability is best-effort, but a successfully parsed API
  // description must belong to this product. Do not swallow our own target
  // mismatch error in the network/parse fallback.
  let openapiResponse: Response;
  try {
    openapiResponse = await fetch(`${apiURL}/openapi.json`);
  } catch {
    return;
  }
  if (!openapiResponse.ok) return;
  const text = await openapiResponse.text();
  if (!text.startsWith("{")) return;
  let openapi: { info?: { title?: string }; paths?: Record<string, unknown> };
  try {
    openapi = JSON.parse(text) as typeof openapi;
  } catch {
    return;
  }
  const paths = new Set(Object.keys(openapi.paths || {}));
  const missing = REQUIRED_PATHS.filter((requiredPath) => !paths.has(requiredPath));
  if (missing.length > 0) {
    throw new Error(`Target API is not ai-gateway. Missing paths: ${missing.join(", ")}`);
  }
}

function passwordFromEnvLine(line: string): string | null {
  const trimmed = line.trim();
  if (!trimmed || trimmed.startsWith("#") || !trimmed.startsWith("DEFAULT_USER_PASSWORD=")) {
    return null;
  }
  let value = trimmed.slice("DEFAULT_USER_PASSWORD=".length);
  if (
    (value.startsWith('"') && value.endsWith('"')) ||
    (value.startsWith("'") && value.endsWith("'"))
  ) {
    value = value.slice(1, -1);
  }
  return value || null;
}

async function detectDefaultPassword(): Promise<string> {
  const fromEnv = process.env.DEFAULT_USER_PASSWORD || process.env.E2E_BOOTSTRAP_PASSWORD;
  if (fromEnv) {
    return fromEnv;
  }

  try {
    const envText = await fs.readFile(path.join(repoRoot(), ".env"), "utf-8");
    for (const line of envText.split("\n")) {
      const value = passwordFromEnvLine(line);
      if (value) {
        return value;
      }
    }
  } catch {
    // .env is optional when the password is already in the process environment.
  }

  throw new Error(
    "DEFAULT_USER_PASSWORD is not set. Export it or add it to .env for Playwright setup."
  );
}

async function detectBootstrapPasswords(defaultPassword: string): Promise<string[]> {
  const candidates = new Set<string>();

  if (process.env.E2E_BOOTSTRAP_PASSWORD) {
    candidates.add(process.env.E2E_BOOTSTRAP_PASSWORD);
  }

  candidates.add(defaultPassword);

  const migrationPath = path.join(
    repoRoot(),
    "database/migrations/005_account_permission_system.sql"
  );
  const migration = await fs.readFile(migrationPath, "utf-8").catch(() => "");
  const inlineCommentMatch = migration.match(/Password:\s*([^\s]+)/);
  const hashCommentMatch = migration.match(/bcrypt hash of '([^']+)'/);

  if (inlineCommentMatch?.[1]) {
    candidates.add(inlineCommentMatch[1]);
  }
  if (hashCommentMatch?.[1]) {
    candidates.add(hashCommentMatch[1]);
  }

  return Array.from(candidates);
}

function buildNextPassword(): string {
  return `Agw!${Date.now().toString(36)}Z9`;
}

async function login(apiURL: string, email: string, password: string) {
  const response = await fetch(`${apiURL}/api/v1/auth/login`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ email, password }),
  });

  const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) {
    throw new Error(`E2E login failed (${response.status}): ${JSON.stringify(payload)}`);
  }
  return payload;
}

async function loginWithCandidates(
  apiURL: string,
  email: string,
  passwords: string[]
): Promise<{ password: string; payload: Record<string, unknown> }> {
  let lastError: Error | null = null;

  for (const password of passwords) {
    try {
      const payload = await login(apiURL, email, password);
      return { password, payload };
    } catch (error) {
      lastError = error as Error;
    }
  }

  throw lastError || new Error(`Unable to authenticate bootstrap user: ${email}`);
}

async function createE2EAdminUser(
  apiURL: string,
  bootstrapToken: string,
  email: string
) {
  const response = await fetch(`${apiURL}/api/v1/users`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      Authorization: `Bearer ${bootstrapToken}`,
    },
    body: JSON.stringify({
      email,
      display_name: "Playwright E2E",
      roles: ["admin"],
    }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Failed to provision E2E admin user (${response.status}): ${body}`);
  }
}

async function updateE2EUser(
  apiURL: string,
  adminToken: string,
  userId: string,
  body: Record<string, unknown>
) {
  const response = await fetch(`${apiURL}/api/v1/users/${userId}`, {
    method: "PUT",
    headers: {
      "content-type": "application/json",
      Authorization: `Bearer ${adminToken}`,
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Failed to update E2E user ${userId} (${response.status}): ${text}`);
  }
}

async function resetE2EUserPassword(
  apiURL: string,
  adminToken: string,
  userId: string
) {
  const response = await fetch(`${apiURL}/api/v1/users/${userId}/reset-password`, {
    method: "POST",
    headers: { Authorization: `Bearer ${adminToken}` },
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Failed to reset E2E user ${userId} (${response.status}): ${text}`);
  }
}

async function createOrResetModelTesterUsers(
  apiURL: string,
  adminToken: string,
  authEmailDomain: string,
  defaultPassword: string
) {
  for (let index = 1; index <= 5; index += 1) {
    const userId = `model_tester_${index}`;
    const email = `${userId}@${authEmailDomain}`;
    const displayName = `Model Tester ${index}`;

    const createResponse = await fetch(`${apiURL}/api/v1/users`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        Authorization: `Bearer ${adminToken}`,
      },
      body: JSON.stringify({
        email,
        display_name: displayName,
        roles: ["model_tester"],
      }),
    });

    if (!createResponse.ok && createResponse.status !== 400) {
      const text = await createResponse.text();
      throw new Error(`Failed to provision ${email} (${createResponse.status}): ${text}`);
    }

    await updateE2EUser(apiURL, adminToken, userId, {
      display_name: displayName,
      roles: ["model_tester"],
      status: "active",
    });
    await resetE2EUserPassword(apiURL, adminToken, userId);

    let loginPayload = await login(apiURL, email, defaultPassword);
    if (defaultPassword === MODEL_TESTER_PASSWORD && loginPayload.force_password_change === true) {
      const temporaryPassword = `${MODEL_TESTER_PASSWORD}-Tmp1!`;
      await changePassword(
        apiURL,
        String(loginPayload.access_token || ""),
        defaultPassword,
        temporaryPassword
      );
      loginPayload = await login(apiURL, email, temporaryPassword);
      await changePassword(
        apiURL,
        String(loginPayload.access_token || ""),
        temporaryPassword,
        MODEL_TESTER_PASSWORD
      );
    } else if (
      defaultPassword !== MODEL_TESTER_PASSWORD ||
      loginPayload.force_password_change === true
    ) {
      await changePassword(
        apiURL,
        String(loginPayload.access_token || ""),
        defaultPassword,
        MODEL_TESTER_PASSWORD
      );
    }
  }
}

async function changePassword(
  apiURL: string,
  token: string,
  currentPassword: string,
  nextPassword: string
) {
  const response = await fetch(`${apiURL}/api/v1/auth/change-password`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: nextPassword,
      confirm_password: nextPassword,
    }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`E2E password change failed (${response.status}): ${body}`);
  }
}

async function validateToken(apiURL: string, token: string) {
  const response = await fetch(`${apiURL}/api/v1/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Token validation failed (${response.status}): ${body}`);
  }
  return response.json();
}

export default async function globalSetup(config: FullConfig) {
  const baseURL = String(config.projects[0].use.baseURL);
  const storageStatePath = String(config.projects[0].use.storageState);
  const apiURL = process.env.E2E_API_URL;

  if (!apiURL) {
    throw new Error("Missing E2E_API_URL for Playwright global setup.");
  }

  await fs.mkdir(ARTIFACT_DIR, { recursive: true });
  await verifyApi(apiURL);

  const defaultPassword = await detectDefaultPassword();
  const providedEmail = process.env.E2E_USER_EMAIL;
  const providedPassword = process.env.E2E_USER_PASSWORD;
  const authEmailDomain = process.env.E2E_AUTH_EMAIL_DOMAIN || "example.com";
  const email = providedEmail || `assistant.e2e.${Date.now()}@${authEmailDomain}`;
  let password = providedPassword || defaultPassword;
  let loginPayload: Record<string, unknown>;
  let provisioningToken: string | null = null;

  if (providedEmail && providedPassword) {
    loginPayload = await login(apiURL, email, password);
  } else {
    const bootstrapEmail = process.env.E2E_BOOTSTRAP_EMAIL || `admin@${authEmailDomain}`;
    const bootstrapPasswords = await detectBootstrapPasswords(defaultPassword);
    const bootstrapLogin = await loginWithCandidates(apiURL, bootstrapEmail, bootstrapPasswords);
    provisioningToken = String(bootstrapLogin.payload.access_token || "");

    await createE2EAdminUser(
      apiURL,
      provisioningToken,
      email
    );

    loginPayload = await login(apiURL, email, defaultPassword);
    password = defaultPassword;
  }

  if (loginPayload.force_password_change === true) {
    const nextPassword = buildNextPassword();
    await changePassword(apiURL, String(loginPayload.access_token), password, nextPassword);
    password = nextPassword;
    loginPayload = await login(apiURL, email, password);
  }

  const token = String(loginPayload.access_token || "");
  if (!token) {
    throw new Error("Login succeeded but access_token is missing.");
  }

  const currentUser = await validateToken(apiURL, token);
  await createOrResetModelTesterUsers(
    apiURL,
    provisioningToken || token,
    authEmailDomain,
    defaultPassword
  );
  const authPayload = {
    state: {
      token,
      user: currentUser,
      isAuthenticated: true,
      forcePasswordChange: Boolean(loginPayload.force_password_change),
      rememberMe: true,
    },
    version: 0,
  };

  await fs.writeFile(USER_FILE, JSON.stringify({ email, password }, null, 2));

  const browser = await chromium.launch();
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await context.newPage();
  await page.goto(`${baseURL}/login`, { waitUntil: "domcontentloaded" });
  await page.evaluate(
    ({ authStorageKey, appStorageKey, authState }) => {
      localStorage.setItem(authStorageKey, JSON.stringify(authState));
      localStorage.setItem("i18nextLng", "zh-CN");
      localStorage.setItem(
        appStorageKey,
        JSON.stringify({
          state: {
            themeMode: "system",
            resolvedTheme: "light",
            darkMode: false,
          },
          version: 3,
        })
      );
    },
    {
      authStorageKey: AUTH_STORAGE_KEY,
      appStorageKey: APP_STORAGE_KEY,
      authState: authPayload,
    }
  );
  await page.goto(`${baseURL}/dashboard`, { waitUntil: "domcontentloaded" });
  await page.waitForURL(/\/dashboard/);
  // The setup page's in-memory auth store may finish hydrating after the
  // direct localStorage seed and rewrite it.  Persist the validated token once
  // more immediately before capturing storage state so the test context starts
  // from the same authenticated state that was verified above.
  await page.evaluate(
    ({ authStorageKey, authState }) => {
      localStorage.setItem(authStorageKey, JSON.stringify(authState));
    },
    { authStorageKey: AUTH_STORAGE_KEY, authState: authPayload }
  );
  await context.storageState({ path: storageStatePath });
  await browser.close();

  await fs.writeFile(
    path.join(ARTIFACT_DIR, "stack.json"),
    JSON.stringify({ baseURL, apiURL, userId: currentUser.user_id, email }, null, 2)
  );
}
