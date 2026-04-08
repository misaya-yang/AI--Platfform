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

/** Islamic 8-pointed star tessellation — SVG pattern */
function IslamicPattern() {
  return (
    <svg className="absolute inset-0 w-full h-full" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <pattern id="islamic-geo" x="0" y="0" width="80" height="80" patternUnits="userSpaceOnUse">
          {/* 8-pointed star */}
          <polygon
            points="40,4 47,17 62,10 53,24 68,24 56,33 68,40 56,47 68,56 53,56 62,70 47,63 40,76 33,63 18,70 27,56 12,56 24,47 12,40 24,33 12,24 27,24 18,10 33,17"
            fill="none"
            stroke="currentColor"
            strokeWidth="0.5"
            opacity="0.12"
          />
          {/* Inner octagon */}
          <polygon
            points="40,17 53,24 56,40 53,56 40,63 27,56 24,40 27,24"
            fill="none"
            stroke="currentColor"
            strokeWidth="0.5"
            opacity="0.08"
          />
          {/* Center diamond */}
          <polygon
            points="40,28 52,40 40,52 28,40"
            fill="currentColor"
            opacity="0.04"
          />
        </pattern>
      </defs>
      <rect width="100%" height="100%" fill="url(#islamic-geo)" />
    </svg>
  );
}

export function LoginPage() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const { setAuth, setLoading, isLoading } = useAuthStore();
  const allowedDomain = "hejazfs.com.au";
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
      if (res.force_password_change) setShowPasswordChange(true); else navigate("/");
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
          <p className="mt-2 text-sm text-muted-foreground">{t("login.forgotPasswordModal.adminEmail", { email: "admin@hejazfs.com.au" })}</p>
        </div>
      ),
      okText: t("login.forgotPasswordModal.ok"),
    });
  };

  return (
    <div className="min-h-screen flex flex-col lg:flex-row">
      {/* ── Left: Hero with Islamic geometric pattern ── */}
      <div className="hidden lg:flex lg:w-[52%] relative overflow-hidden bg-[#1a4731] text-[#f0faf4]">
        {/* Pattern overlay */}
        <IslamicPattern />

        {/* Warm gold accent line at top */}
        <div className="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-transparent via-[#c9a84c] to-transparent opacity-60" />

        {/* Content */}
        <div className="relative z-10 flex flex-col justify-between p-12 w-full">
          {/* Logo area */}
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-md bg-[#c9a84c]/20 border border-[#c9a84c]/30 flex items-center justify-center">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 2L2 7l10 5 10-5-10-5Z" />
                <path d="M2 17l10 5 10-5" />
                <path d="M2 12l10 5 10-5" />
              </svg>
            </div>
            <span className="text-base font-semibold tracking-tight">Hejaz AI</span>
          </div>

          {/* Center text */}
          <div className="max-w-md">
            <h1 className="text-3xl font-semibold leading-tight tracking-tight mb-4">
              {t("login.heroTitle", "Islamic Knowledge, Amplified by Intelligence")}
            </h1>
            <p className="text-base leading-relaxed opacity-70">
              {t("login.heroSubtitle", "An enterprise AI platform purpose-built for Islamic education, research, and scholarly dialogue.")}
            </p>
          </div>

          {/* Bottom decorative element */}
          <div className="flex items-center gap-2 text-xs opacity-40">
            <div className="w-8 h-[1px] bg-current" />
            <span className="font-arabic text-sm">بسم الله الرحمن الرحيم</span>
            <div className="w-8 h-[1px] bg-current" />
          </div>
        </div>
      </div>

      {/* ── Right: Login form ── */}
      <div className="flex-1 flex items-center justify-center bg-background px-6 py-12 lg:px-12">
        <div className="w-full max-w-[380px]">
          {/* Mobile logo — only shows on small screens */}
          <div className="flex items-center gap-2.5 mb-10 lg:hidden">
            <div className="w-8 h-8 rounded-md bg-primary/10 border border-primary/15 flex items-center justify-center">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="text-primary">
                <path d="M12 2L2 7l10 5 10-5-10-5Z" />
                <path d="M2 17l10 5 10-5" />
                <path d="M2 12l10 5 10-5" />
              </svg>
            </div>
            <span className="font-semibold text-sm text-foreground tracking-tight">Hejaz AI</span>
          </div>

          {/* Form header */}
          <div className="mb-8">
            <h2 className="text-xl font-semibold text-foreground tracking-tight">{t("login.title")}</h2>
            <p className="text-sm text-muted-foreground mt-1.5">{t("login.loginWith")}</p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-5">
            <div className="space-y-1.5">
              <Label htmlFor="email" className="text-sm font-medium">{t("login.emailLabel")}</Label>
              <div className="relative">
                <Input
                  id="email" type="text" placeholder="username"
                  value={email} onChange={e => setEmail(e.target.value)}
                  required disabled={isLoading}
                  className="h-10 pr-[140px]"
                />
                <span className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground text-[13px] pointer-events-none select-none">
                  @{allowedDomain}
                </span>
              </div>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="password" className="text-sm font-medium">{t("login.passwordLabel")}</Label>
              <div className="relative">
                <Input
                  id="password" type={showPassword ? "text" : "password"}
                  placeholder={t("login.passwordPlaceholder")}
                  value={password} onChange={e => setPassword(e.target.value)}
                  required disabled={isLoading}
                  className="h-10 pr-10"
                />
                <button
                  type="button"
                  aria-label={showPassword ? t("login.hidePassword") : t("login.showPassword")}
                  onClick={() => setShowPassword(p => !p)}
                  className="absolute right-2.5 top-1/2 -translate-y-1/2 p-1 text-muted-foreground hover:text-foreground transition-colors rounded"
                >
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
              <div className="text-center text-sm text-destructive bg-destructive/6 border border-destructive/10 rounded-md py-2.5 px-3">
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

      <PasswordChangeModal open={showPasswordChange} onComplete={() => { setShowPasswordChange(false); navigate("/"); }} />
    </div>
  );
}
