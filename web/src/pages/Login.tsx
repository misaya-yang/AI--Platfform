import { useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { motion, useReducedMotion } from "framer-motion";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { getAllowedEmailDomain, getSupportEmail } from "@/config/runtime";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Logo } from "@/components/Logo";
import { useAuthStore } from "@/store/useAuthStore";
import { login } from "@/api/auth";
import { PasswordChangeModal } from "@/components/PasswordChangeModal";
import { Activity, BookOpen, Eye, EyeOff, Server, ShieldCheck } from "lucide-react";
import { Modal } from "antd";
import type { AxiosError } from "axios";

type ApiValidationIssue = { msg?: unknown; message?: unknown };
type ApiErrorPayload = { detail?: unknown; message?: unknown; error?: unknown };

function sanitizeMessage(value: string): string {
  return value.replace(/^Value error,\s*/i, "").trim();
}

function extractErrorText(payload: unknown): string | null {
  if (!payload) return null;
  if (typeof payload === "string") { const t = sanitizeMessage(payload); return t || null; }
  if (Array.isArray(payload)) {
    const msgs = payload.map(item => {
      if (typeof item === "string") return sanitizeMessage(item);
      if (!item || typeof item !== "object") return "";
      const i = item as ApiValidationIssue;
      if (typeof i.msg === "string") return sanitizeMessage(i.msg);
      if (typeof i.message === "string") return sanitizeMessage(i.message);
      return "";
    }).filter(Boolean);
    return msgs.length > 0 ? msgs.join("; ") : null;
  }
  if (typeof payload === "object") {
    const o = payload as ApiValidationIssue;
    if (typeof o.msg === "string") return sanitizeMessage(o.msg);
    if (typeof o.message === "string") return sanitizeMessage(o.message);
  }
  return null;
}

function getSafeReturnPath(state: unknown): string | null {
  if (!state || typeof state !== "object") return null;
  const from = (state as { from?: unknown }).from;
  if (!from || typeof from !== "object") return null;

  const { pathname, search, hash } = from as {
    pathname?: unknown;
    search?: unknown;
    hash?: unknown;
  };
  if (
    typeof pathname !== "string"
    || !pathname.startsWith("/")
    || pathname.startsWith("//")
    || pathname === "/login"
  ) {
    return null;
  }

  const safeSearch = typeof search === "string" && (search === "" || search.startsWith("?"))
    ? search
    : "";
  const safeHash = typeof hash === "string" && (hash === "" || hash.startsWith("#"))
    ? hash
    : "";
  return `${pathname}${safeSearch}${safeHash}`;
}

function getDefaultDestination(permissions: string[]): string {
  const hasDashboard = permissions.includes("console:dashboard:view") || permissions.includes("admin:*");
  const hasPlayground = permissions.includes("conversation:playground:access") || permissions.includes("admin:*");
  return !hasDashboard && hasPlayground ? "/playground" : "/";
}

export function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { t } = useTranslation();
  const { setAuth, setLoading, isLoading } = useAuthStore();
  const shouldReduceMotion = useReducedMotion();
  const allowedDomain = getAllowedEmailDomain();
  const supportEmail = getSupportEmail();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [showPasswordChange, setShowPasswordChange] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [rememberMe, setRememberMe] = useState(false);
  const returnPath = getSafeReturnPath(location.state);
  const hasFullEmail = email.trim().includes("@");

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault(); setError(""); setLoading(true);
    try {
      const trimmed = email.trim();
      if (!trimmed) { setError(t("login.errors.emailRequired")); setLoading(false); return; }
      const normalized = trimmed.includes("@") ? trimmed.toLowerCase() : `${trimmed.toLowerCase()}@${allowedDomain}`;
      const res = await login({ email: normalized, password });
      setAuth(res.access_token, res.user, res.force_password_change, rememberMe);
      if (res.force_password_change) {
        setShowPasswordChange(true);
      } else {
        navigate(returnPath || getDefaultDestination(res.user.permissions || []), { replace: true });
      }
    } catch (err: unknown) {
      const ax = err as AxiosError<ApiErrorPayload>;
      const status = ax.response?.status;
      const data = ax.response?.data;
      const msg = extractErrorText(data?.detail ?? data);
      if (status === 423) setError(msg || t("login.errors.accountLocked"));
      else if (status === 401) setError(t("login.errors.invalidCredentials"));
      else if (status === 403) setError(t("login.errors.accountDisabled"));
      else setError(msg || t("login.errors.loginFailed"));
    } finally { setLoading(false); }
  };

  const handleForgotPassword = () => {
    Modal.info({
      title: t("login.forgotPasswordModal.title"),
      content: (
        <div className="py-2">
          <p>{t("login.forgotPasswordModal.content")}</p>
          <p className="mt-2 text-sm text-muted-foreground">{t("login.forgotPasswordModal.adminEmail", { email: supportEmail })}</p>
        </div>
      ),
      okText: t("login.forgotPasswordModal.ok"),
    });
  };

  const highlights = [
    {
      icon: Server,
      title: t("login.highlights.operations.title"),
      description: t("login.highlights.operations.description"),
    },
    {
      icon: BookOpen,
      title: t("login.highlights.knowledge.title"),
      description: t("login.highlights.knowledge.description"),
    },
    {
      icon: Activity,
      title: t("login.highlights.observability.title"),
      description: t("login.highlights.observability.description"),
    },
  ];
  const entranceTransition = {
    duration: shouldReduceMotion ? 0 : 0.38,
    ease: [0.16, 1, 0.3, 1] as const,
  };

  return (
    <div className="min-h-dvh bg-background">
      <main className="mx-auto grid min-h-dvh w-full max-w-[1320px] lg:grid-cols-[minmax(0,1.08fr)_minmax(420px,0.92fr)]">
        <motion.aside
          initial={shouldReduceMotion ? false : { opacity: 0, x: -12 }}
          animate={{ opacity: 1, x: 0 }}
          transition={entranceTransition}
          className="relative hidden min-h-dvh flex-col border-r border-border bg-muted/20 px-12 py-10 lg:flex xl:px-16 xl:py-12"
          aria-labelledby="login-brand-title"
        >
          <Logo collapsed={false} />

          <div className="my-auto max-w-xl py-14">
            <p className="mb-4 text-xs font-medium uppercase tracking-[0.18em] text-primary">
              {t("login.brandEyebrow")}
            </p>
            <h2 id="login-brand-title" className="max-w-lg text-4xl font-semibold leading-[1.08] tracking-[-0.035em] text-foreground xl:text-5xl">
              {t("login.brandTitle")}
            </h2>
            <p className="mt-5 max-w-lg text-base leading-7 text-muted-foreground">
              {t("login.brandDescription")}
            </p>

            <div className="mt-10 border-y border-border">
              {highlights.map(({ icon: Icon, title, description }) => (
                <div key={title} className="grid grid-cols-[32px_1fr] gap-4 border-b border-border py-5 last:border-b-0">
                  <div className="flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-background text-primary">
                    <Icon aria-hidden="true" size={16} strokeWidth={1.6} />
                  </div>
                  <div>
                    <h3 className="text-sm font-medium text-foreground">{title}</h3>
                    <p className="mt-1 text-sm leading-5 text-muted-foreground">{description}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <ShieldCheck aria-hidden="true" size={15} strokeWidth={1.7} />
            <span>{t("login.secureAccess")}</span>
          </div>
        </motion.aside>

        <section className="flex min-h-dvh items-center justify-center px-6 py-10 sm:px-10 lg:px-14">
          <motion.div
            initial={shouldReduceMotion ? false : { opacity: 0, x: 12 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ ...entranceTransition, delay: shouldReduceMotion ? 0 : 0.06 }}
            className="w-full max-w-[420px]"
          >
            <div className="mb-10 lg:hidden">
              <Logo collapsed={false} />
            </div>

            <div className="mb-8">
              <p className="mb-3 text-xs font-medium uppercase tracking-[0.16em] text-primary">
                {t("login.brandEyebrow")}
              </p>
              <h1 className="text-3xl font-semibold tracking-tight text-foreground">{t("login.title")}</h1>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">{t("login.loginWith")}</p>
            </div>

            <form onSubmit={handleSubmit} className="space-y-5">
              <div className="space-y-1.5">
                <Label htmlFor="email" className="text-sm font-medium">{t("login.emailLabel")}</Label>
                <div className="flex h-11 items-stretch overflow-hidden rounded-md border border-input bg-background transition-[border-color,box-shadow] duration-150 focus-within:border-primary/50 focus-within:ring-[3px] focus-within:ring-ring/10">
                  <Input
                    id="email"
                    type="text"
                    inputMode="email"
                    autoComplete="username"
                    autoCapitalize="none"
                    spellCheck={false}
                    placeholder={t("login.emailPlaceholder")}
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    required
                    disabled={isLoading}
                    aria-invalid={Boolean(error)}
                    aria-describedby={error ? "login-email-hint login-error" : "login-email-hint"}
                    className="h-full w-0 min-w-0 flex-1 rounded-none border-0 bg-transparent shadow-none focus-visible:border-0 focus-visible:ring-0"
                  />
                  {!hasFullEmail && (
                    <span
                      className="flex max-w-[55%] shrink-0 items-center border-l border-border bg-muted/30 px-3 text-[13px] text-muted-foreground"
                      title={`@${allowedDomain}`}
                      aria-hidden="true"
                    >
                      <span className="truncate">@{allowedDomain}</span>
                    </span>
                  )}
                </div>
                <p id="login-email-hint" className="sr-only">
                  {t("login.emailHint", { domain: allowedDomain })}
                </p>
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="password" className="text-sm font-medium">{t("login.passwordLabel")}</Label>
                <div className="relative">
                  <Input
                    id="password"
                    type={showPassword ? "text" : "password"}
                    autoComplete="current-password"
                    placeholder={t("login.passwordPlaceholder")}
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    required
                    disabled={isLoading}
                    aria-invalid={Boolean(error)}
                    aria-describedby={error ? "login-error" : undefined}
                    className="h-11 pr-11"
                  />
                  <button
                    type="button"
                    aria-label={showPassword ? t("login.hidePassword") : t("login.showPassword")}
                    onClick={() => setShowPassword((current) => !current)}
                    className="absolute right-2.5 top-1/2 -translate-y-1/2 rounded p-1.5 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/30"
                  >
                    {showPassword ? <EyeOff aria-hidden="true" size={16} /> : <Eye aria-hidden="true" size={16} />}
                  </button>
                </div>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
                <label htmlFor="remember-me" className="flex cursor-pointer items-center gap-2 text-muted-foreground">
                  <Checkbox id="remember-me" checked={rememberMe} onCheckedChange={(checked) => setRememberMe(checked === true)} />
                  {t("login.rememberMeDays")}
                </label>
                <button
                  type="button"
                  onClick={handleForgotPassword}
                  className="rounded text-[13px] text-primary transition-colors hover:text-primary/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/30"
                >
                  {t("login.forgotPassword")}
                </button>
              </div>

              {error && (
                <div
                  id="login-error"
                  role="alert"
                  aria-live="polite"
                  className="rounded-md border border-destructive/15 bg-destructive/6 px-3 py-2.5 text-sm text-destructive"
                >
                  {error}
                </div>
              )}

              <Button
                type="submit"
                variant="primary"
                className="h-11 w-full text-sm font-medium"
                disabled={isLoading}
              >
                {isLoading ? t("login.loggingIn") : t("login.loginButton")}
              </Button>

              <p className="pt-1 text-center text-xs text-muted-foreground">
                {t("login.enterpriseOnly", { domain: allowedDomain })}
              </p>
            </form>
          </motion.div>
        </section>
      </main>

      <PasswordChangeModal open={showPasswordChange} onComplete={() => {
        setShowPasswordChange(false);
        const user = useAuthStore.getState().user;
        navigate(returnPath || getDefaultDestination(user?.permissions || []), { replace: true });
      }} />
    </div>
  );
}
