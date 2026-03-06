import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium, type FullConfig } from "@playwright/test";

const AUTH_STORAGE_KEY = "agent-gateway-auth";
const APP_STORAGE_KEY = "agent-gateway-storage";
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
  const [healthResponse, openapiResponse] = await Promise.all([
    fetch(`${apiURL}/health`),
    fetch(`${apiURL}/openapi.json`),
  ]);

  if (!healthResponse.ok) {
    throw new Error(`Health check failed (${healthResponse.status})`);
  }
  if (!openapiResponse.ok) {
    throw new Error(`OpenAPI check failed (${openapiResponse.status})`);
  }

  const openapi = (await openapiResponse.json()) as {
    info?: { title?: string };
    paths?: Record<string, unknown>;
  };
  const paths = new Set(Object.keys(openapi.paths || {}));
  const missing = REQUIRED_PATHS.filter((requiredPath) => !paths.has(requiredPath));
  if (missing.length > 0) {
    throw new Error(`Target API is not ai-gateway. Missing paths: ${missing.join(", ")}`);
  }
}

async function detectDefaultPassword(): Promise<string> {
  const passwordModule = await fs.readFile(
    path.join(repoRoot(), "src/core/auth/password.py"),
    "utf-8"
  );
  const match = passwordModule.match(/DEFAULT_PASSWORD\s*=\s*["']([^"']+)["']/);
  if (!match) {
    throw new Error("Unable to detect DEFAULT_PASSWORD from src/core/auth/password.py");
  }
  return match[1];
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
  const email = providedEmail || `assistant.e2e.${Date.now()}@hejazfs.com.au`;
  let password = providedPassword || defaultPassword;
  let loginPayload: Record<string, unknown>;

  if (providedEmail && providedPassword) {
    loginPayload = await login(apiURL, email, password);
  } else {
    const bootstrapEmail = process.env.E2E_BOOTSTRAP_EMAIL || "admin@hejazfs.com.au";
    const bootstrapPasswords = await detectBootstrapPasswords(defaultPassword);
    const bootstrapLogin = await loginWithCandidates(apiURL, bootstrapEmail, bootstrapPasswords);

    await createE2EAdminUser(
      apiURL,
      String(bootstrapLogin.payload.access_token || ""),
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
  const authPayload = {
    state: {
      token,
      user: loginPayload.user,
      isAuthenticated: true,
      forcePasswordChange: Boolean(loginPayload.force_password_change),
      rememberMe: true,
    },
    version: 0,
  };

  await fs.writeFile(USER_FILE, JSON.stringify({ email, password }, null, 2));

  const browser = await chromium.launch();
  const context = await browser.newContext();
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
  await context.storageState({ path: storageStatePath });
  await browser.close();

  await fs.writeFile(
    path.join(ARTIFACT_DIR, "stack.json"),
    JSON.stringify({ baseURL, apiURL, userId: currentUser.user_id, email }, null, 2)
  );
}
