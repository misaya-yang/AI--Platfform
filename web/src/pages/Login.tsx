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

type ApiValidationIssue = {
  msg?: unknown;
  message?: unknown;
};

type ApiErrorPayload = {
  detail?: unknown;
  message?: unknown;
  error?: unknown;
};

function sanitizeMessage(value: string): string {
  return value.replace(/^Value error,\s*/i, "").trim();
}

function extractErrorText(payload: unknown): string | null {
  if (!payload) return null;

  if (typeof payload === "string") {
    const text = sanitizeMessage(payload);
    return text || null;
  }

  if (Array.isArray(payload)) {
    const messages = payload
      .map((item) => {
        if (typeof item === "string") return sanitizeMessage(item);
        if (!item || typeof item !== "object") return "";
        const issue = item as ApiValidationIssue;
        if (typeof issue.msg === "string") return sanitizeMessage(issue.msg);
        if (typeof issue.message === "string") return sanitizeMessage(issue.message);
        return "";
      })
      .filter(Boolean);

    return messages.length > 0 ? messages.join("; ") : null;
  }

  if (typeof payload === "object") {
    const obj = payload as ApiValidationIssue;
    if (typeof obj.msg === "string") return sanitizeMessage(obj.msg);
    if (typeof obj.message === "string") return sanitizeMessage(obj.message);
  }

  return null;
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
    e.preventDefault();
    setError("");
    setLoading(true);

    try {
      const trimmedEmail = email.trim();
      if (!trimmedEmail) {
        setError(t("login.errors.emailRequired"));
        setLoading(false);
        return;
      }
      const normalizedEmail = trimmedEmail.includes("@")
        ? trimmedEmail.toLowerCase()
        : `${trimmedEmail.toLowerCase()}@${allowedDomain}`;

      const response = await login({ email: normalizedEmail, password });

      setAuth(response.access_token, response.user, response.force_password_change, rememberMe);

      if (response.force_password_change) {
        setShowPasswordChange(true);
      } else {
        navigate("/");
      }
    } catch (err: unknown) {
      const axiosError = err as AxiosError<ApiErrorPayload>;
      const status = axiosError.response?.status;
      const errorPayload = axiosError.response?.data;
      const parsedMessage = extractErrorText(errorPayload?.detail ?? errorPayload);

      if (status === 423) {
        setError(parsedMessage || t("login.errors.accountLocked"));
      } else if (status === 401) {
        setError(t("login.errors.invalidCredentials"));
      } else if (status === 403) {
        setError(t("login.errors.accountDisabled"));
      } else {
        setError(parsedMessage || t("login.errors.loginFailed"));
      }
    } finally {
      setLoading(false);
    }
  };

  const handlePasswordChangeComplete = () => {
    setShowPasswordChange(false);
    navigate("/");
  };

  const handleForgotPassword = () => {
    Modal.info({
      title: t("login.forgotPasswordModal.title"),
      content: (
        <div className="py-2">
          <p>{t("login.forgotPasswordModal.content")}</p>
          <p className="mt-2 text-sm text-gray-500">
            {t("login.forgotPasswordModal.adminEmail", { email: "admin@hejazfs.com.au" })}
          </p>
        </div>
      ),
      okText: t("login.forgotPasswordModal.ok"),
    });
  };

  return (
    <div className="login-shell relative min-h-screen overflow-hidden">
      {/* Background */}
      <div className="login-background" />

      <div className="relative z-10 flex min-h-screen items-center justify-center px-4 py-12">
        <div className="w-full max-w-[440px]">
          {/* Brand chip */}
          <div className="mb-8 flex items-center justify-center">
            <span className="brand-chip">AI Platform</span>
          </div>

          {/* Login card */}
          <div className="login-card rounded-2xl border border-slate-200/80 bg-white/95 px-8 pb-10 pt-8 shadow-[0_20px_50px_rgba(30,41,59,0.08)] backdrop-blur-sm sm:px-10 sm:pt-10 dark:border-white/[0.08] dark:bg-[#1A1D27]/95 dark:shadow-[0_20px_50px_rgba(0,0,0,0.3)]">
            <div className="text-center">
              <h1 className="text-xl font-semibold tracking-tight text-slate-800 dark:text-slate-100">
                {t("login.title")}
              </h1>
              <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">
                {t("login.loginWith")}
              </p>
            </div>

            <form onSubmit={handleSubmit} className="mt-8 space-y-5">
              <div className="space-y-2">
                <Label htmlFor="email" className="text-sm text-slate-600 dark:text-slate-300">
                  {t("login.emailLabel")}
                </Label>
                <div className="relative">
                  <Input
                    id="email"
                    type="text"
                    placeholder="username"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    required
                    disabled={isLoading}
                    className="login-input pr-[140px]"
                  />
                  <span className="email-suffix">@{allowedDomain}</span>
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="password" className="text-sm text-slate-600 dark:text-slate-300">
                  {t("login.passwordLabel")}
                </Label>
                <div className="relative">
                  <Input
                    id="password"
                    type={showPassword ? "text" : "password"}
                    placeholder={t("login.passwordPlaceholder")}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    required
                    disabled={isLoading}
                    className="login-input pr-12"
                  />
                  <button
                    type="button"
                    aria-label={showPassword ? t("login.hidePassword") : t("login.showPassword")}
                    onClick={() => setShowPassword((prev) => !prev)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 rounded-full p-1 text-slate-400 transition hover:text-slate-600 dark:text-slate-500 dark:hover:text-slate-300"
                  >
                    {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                  </button>
                </div>
              </div>

              <div className="flex items-center justify-between text-sm text-slate-500 dark:text-slate-400">
                <label className="flex items-center gap-2 cursor-pointer">
                  <Checkbox
                    checked={rememberMe}
                    onCheckedChange={(checked) => setRememberMe(checked === true)}
                  />
                  {t("login.rememberMeDays")}
                </label>
                <button
                  type="button"
                  onClick={handleForgotPassword}
                  className="text-indigo-500 hover:text-indigo-600 transition dark:text-indigo-400 dark:hover:text-indigo-300"
                >
                  {t("login.forgotPassword")}
                </button>
              </div>

              {error && (
                <div className="text-center text-sm text-rose-500 dark:text-rose-400">
                  {error}
                </div>
              )}

              <Button
                type="submit"
                className="login-submit-btn h-11 w-full rounded-lg text-sm font-medium text-white transition-all"
                disabled={isLoading}
              >
                {isLoading ? t("login.loggingIn") : t("login.loginButton")}
              </Button>

              <div className="text-center text-xs text-slate-400 dark:text-slate-500 pt-1">
                {t("login.enterpriseOnly", { domain: allowedDomain })}
              </div>
            </form>
          </div>
        </div>
      </div>

      <PasswordChangeModal
        open={showPasswordChange}
        onComplete={handlePasswordChangeComplete}
      />

      <style>{`
        .login-shell {
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
        }

        .login-background {
          position: absolute;
          inset: 0;
          background: linear-gradient(135deg, #F8FAFC 0%, #EEF2FF 50%, #E0E7FF 100%);
        }

        .dark .login-background {
          background: linear-gradient(135deg, #0F1117 0%, #151825 50%, #0F1117 100%);
        }

        .brand-chip {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          padding: 6px 16px;
          border-radius: 999px;
          background: rgba(255, 255, 255, 0.8);
          border: 1px solid rgba(226, 232, 240, 0.6);
          color: #475569;
          font-size: 11px;
          letter-spacing: 0.2em;
          text-transform: uppercase;
          font-weight: 600;
          backdrop-filter: blur(8px);
        }

        .dark .brand-chip {
          background: rgba(26, 29, 39, 0.8);
          border-color: rgba(255, 255, 255, 0.08);
          color: #94A3B8;
        }

        .login-input {
          height: 44px;
          border-radius: 8px !important;
          border: 1px solid #E2E8F0;
          background: #fff;
          font-size: 14px;
          padding-left: 14px;
          color: #0F172A;
          transition: all 0.2s;
        }

        .dark .login-input {
          border-color: rgba(255, 255, 255, 0.10);
          background: rgba(255, 255, 255, 0.04);
          color: #E2E8F0;
        }

        .login-input:focus-visible {
          border-color: #4F46E5;
          box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.1);
          outline: none;
        }

        .dark .login-input:focus-visible {
          border-color: #818CF8;
          box-shadow: 0 0 0 3px rgba(129, 140, 248, 0.15);
        }

        .email-suffix {
          position: absolute;
          right: 14px;
          top: 50%;
          transform: translateY(-50%);
          font-weight: 500;
          color: #64748B;
          font-size: 13px;
          pointer-events: none;
        }

        .dark .email-suffix {
          color: #64748B;
        }

        .login-submit-btn {
          background: #4F46E5 !important;
          box-shadow: 0 1px 2px rgba(79, 70, 229, 0.2);
        }

        .login-submit-btn:hover:not(:disabled) {
          background: #4338CA !important;
          transform: translateY(-1px);
          box-shadow: 0 4px 12px rgba(79, 70, 229, 0.25);
        }

        .dark .login-submit-btn {
          background: #818CF8 !important;
          box-shadow: 0 1px 2px rgba(129, 140, 248, 0.2);
        }

        .dark .login-submit-btn:hover:not(:disabled) {
          background: #6366F1 !important;
          box-shadow: 0 4px 12px rgba(129, 140, 248, 0.3);
        }
      `}</style>
    </div>
  );
}
