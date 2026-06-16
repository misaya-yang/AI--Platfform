import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useAuthStore } from "@/store/useAuthStore";
import { login } from "@/api/auth";
import { PasswordChangeModal } from "@/components/PasswordChangeModal";
import { Eye, EyeOff } from "lucide-react";
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

export function LoginPage() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { setAuth, setLoading, isLoading } = useAuthStore();
  const allowedDomain = import.meta.env.VITE_AUTH_EMAIL_DOMAIN || "example.com";
  const supportEmail = import.meta.env.VITE_SUPPORT_EMAIL || `admin@${allowedDomain}`;
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [showPasswordChange, setShowPasswordChange] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [rememberMe, setRememberMe] = useState(false);

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
        // Route users without dashboard access to playground instead of 403
        const perms = res.user.permissions || [];
        const hasDashboard = perms.includes("console:dashboard:view") || perms.includes("admin:*");
        const hasPlayground = perms.includes("conversation:playground:access");
        if (!hasDashboard && hasPlayground) {
          navigate("/playground");
        } else {
          navigate("/");
        }
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

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4 py-12">
      <div className="w-full max-w-[400px]">
        {/* Logo */}
        <div className="flex items-center justify-center gap-2.5 mb-10">
          <div className="w-8 h-8 rounded-md bg-primary flex items-center justify-center">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-primary-foreground">
              <path d="M12 2L2 7l10 5 10-5-10-5Z" />
              <path d="M2 17l10 5 10-5" />
              <path d="M2 12l10 5 10-5" />
            </svg>
          </div>
          <span className="font-semibold text-base text-foreground tracking-tight">AI Gateway</span>
        </div>

        {/* Card */}
        <div className="rounded-lg border border-border bg-card p-8">
          <h2 className="text-xl font-semibold text-foreground tracking-tight mb-1">{t("login.title")}</h2>
          <p className="text-sm text-muted-foreground mb-7">{t("login.loginWith")}</p>

          <form onSubmit={handleSubmit} className="space-y-5">
            <div className="space-y-1.5">
              <Label htmlFor="email" className="text-sm font-medium">{t("login.emailLabel")}</Label>
              <div className="relative">
                <Input id="email" type="text" placeholder="username" value={email} onChange={e => setEmail(e.target.value)} required disabled={isLoading}
                  className="h-10 pr-[140px]" />
                <span className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground text-[13px] pointer-events-none select-none">@{allowedDomain}</span>
              </div>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password" className="text-sm font-medium">{t("login.passwordLabel")}</Label>
              <div className="relative">
                <Input id="password" type={showPassword ? "text" : "password"} placeholder={t("login.passwordPlaceholder")} value={password}
                  onChange={e => setPassword(e.target.value)} required disabled={isLoading} className="h-10 pr-10" />
                <button type="button" aria-label={showPassword ? t("login.hidePassword") : t("login.showPassword")} onClick={() => setShowPassword(p => !p)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 p-1 text-muted-foreground hover:text-foreground transition-colors rounded">
                  {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </div>

            <div className="flex items-center justify-between text-sm">
              <label className="flex items-center gap-2 cursor-pointer text-muted-foreground">
                <Checkbox checked={rememberMe} onCheckedChange={c => setRememberMe(c === true)} />
                {t("login.rememberMeDays")}
              </label>
              <button type="button" onClick={handleForgotPassword} className="text-primary hover:text-primary/80 transition-colors text-[13px]">
                {t("login.forgotPassword")}
              </button>
            </div>

            {error && (
              <div className="text-center text-sm text-destructive bg-destructive/[0.06] border border-destructive/10 rounded-md py-2.5 px-3">
                {error}
              </div>
            )}

            <Button type="submit" className="w-full h-10 text-sm font-medium bg-primary text-primary-foreground hover:bg-primary/90 border-0" disabled={isLoading}>
              {isLoading ? t("login.loggingIn") : t("login.loginButton")}
            </Button>

            <p className="text-center text-xs text-muted-foreground pt-1">
              {t("login.enterpriseOnly", { domain: allowedDomain })}
            </p>
          </form>
        </div>
      </div>

      <PasswordChangeModal open={showPasswordChange} onComplete={() => {
        setShowPasswordChange(false);
        const user = useAuthStore.getState().user;
        const perms = user?.permissions || [];
        const hasDashboard = perms.includes("console:dashboard:view") || perms.includes("admin:*");
        const hasPlayground = perms.includes("conversation:playground:access");
        navigate(!hasDashboard && hasPlayground ? "/playground" : "/");
      }} />
    </div>
  );
}
