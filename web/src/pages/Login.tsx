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
import { Eye, EyeOff, Shield, Cpu, Database, Zap } from "lucide-react";
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

const features = [
  { icon: Shield, label: "Enterprise Security" },
  { icon: Cpu, label: "Multi-Model Gateway" },
  { icon: Database, label: "Knowledge Base RAG" },
  { icon: Zap, label: "Real-time Analytics" },
];

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
          <p className="mt-2 text-sm text-gray-500">{t("login.forgotPasswordModal.adminEmail", { email: "admin@hejazfs.com.au" })}</p>
        </div>
      ),
      okText: t("login.forgotPasswordModal.ok"),
    });
  };

  return (
    <div className="login-shell min-h-screen flex">
      {/* Left brand panel */}
      <div className="login-brand hidden lg:flex flex-col justify-between w-[440px] xl:w-[480px] shrink-0 p-10">
        <div>
          <div className="flex items-center gap-2.5 mb-16">
            <div className="w-8 h-8 rounded-lg bg-white/20 flex items-center justify-center">
              <Cpu size={18} className="text-white" />
            </div>
            <span className="text-white font-semibold text-[15px]">AI Platform</span>
          </div>
          <h1 className="text-white text-[32px] leading-tight font-semibold mb-4">
            Unified AI<br />Gateway
          </h1>
          <p className="text-white/60 text-sm leading-relaxed max-w-[320px]">
            Route, observe, and govern all your LLM traffic through a single control plane.
          </p>
        </div>
        <div className="space-y-3">
          {features.map(({ icon: Icon, label }) => (
            <div key={label} className="flex items-center gap-3 text-white/70 text-sm">
              <div className="w-8 h-8 rounded-md bg-white/10 flex items-center justify-center">
                <Icon size={15} className="text-white/80" />
              </div>
              {label}
            </div>
          ))}
        </div>
      </div>

      {/* Right form panel */}
      <div className="flex-1 flex items-center justify-center px-6 py-12 bg-[#f8f9fb] dark:bg-[#0a0a0f]">
        <div className="w-full max-w-[380px]">
          {/* Mobile logo */}
          <div className="lg:hidden flex items-center gap-2 mb-10">
            <div className="w-7 h-7 rounded-md bg-primary flex items-center justify-center">
              <Cpu size={15} className="text-white" />
            </div>
            <span className="font-semibold text-sm text-foreground">AI Platform</span>
          </div>

          <h2 className="text-xl font-semibold text-foreground mb-1">{t("login.title")}</h2>
          <p className="text-sm text-muted-foreground mb-8">{t("login.loginWith")}</p>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="email" className="text-sm text-muted-foreground">{t("login.emailLabel")}</Label>
              <div className="relative">
                <Input id="email" type="text" placeholder="username" value={email} onChange={e => setEmail(e.target.value)} required disabled={isLoading}
                  className="h-10 pr-[140px] bg-card border-border" />
                <span className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground text-[13px] pointer-events-none">@{allowedDomain}</span>
              </div>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="password" className="text-sm text-muted-foreground">{t("login.passwordLabel")}</Label>
              <div className="relative">
                <Input id="password" type={showPassword ? "text" : "password"} placeholder={t("login.passwordPlaceholder")} value={password}
                  onChange={e => setPassword(e.target.value)} required disabled={isLoading} className="h-10 pr-10 bg-card border-border" />
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

            {error && <div className="text-center text-sm text-destructive">{error}</div>}

            <Button type="submit" className="w-full h-10 bg-primary hover:bg-primary/90 text-white text-sm font-medium" disabled={isLoading}>
              {isLoading ? t("login.loggingIn") : t("login.loginButton")}
            </Button>

            <p className="text-center text-xs text-muted-foreground pt-1">
              {t("login.enterpriseOnly", { domain: allowedDomain })}
            </p>
          </form>
        </div>
      </div>

      <PasswordChangeModal open={showPasswordChange} onComplete={() => { setShowPasswordChange(false); navigate("/"); }} />

      <style>{`
        .login-brand {
          background: linear-gradient(160deg, #2e1065 0%, #4c1d95 40%, #6d28d9 100%);
        }
        .dark .login-brand {
          background: linear-gradient(160deg, #1a0533 0%, #2e1065 40%, #4c1d95 100%);
        }
      `}</style>
    </div>
  );
}
