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

      // Set auth state with rememberMe flag
      // rememberMe=true -> localStorage (persist after browser close)
      // rememberMe=false -> sessionStorage (clear on browser close)
      setAuth(response.access_token, response.user, response.force_password_change, rememberMe);

      // Check if password change is required
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
    <div className="login-shell relative min-h-screen overflow-hidden bg-[#f5f8fc]">
      <div className="login-background">
        <div className="login-blob blob-left" />
        <div className="login-blob blob-right" />
        <div className="login-grid" />
      </div>

      <div className="relative z-10 flex min-h-screen items-center justify-center px-4 py-12">
        <div className="w-full max-w-[480px]">
          <div className="mb-6 flex items-center justify-center">
            <span className="brand-chip">AI Platform</span>
          </div>

          <div className="login-card relative overflow-hidden rounded-[24px] border border-[#e2e8f0] bg-white/98 px-8 pb-10 pt-8 shadow-[0_20px_60px_rgba(71,85,105,0.12)] sm:px-10 sm:pt-10">
            <div className="corner-fold" />

            <div className="text-center">
              <h1 className="login-title text-2xl font-semibold text-slate-800">{t("login.title")}</h1>
              <p className="mt-2 text-sm text-slate-500">
                {t("login.loginWith")}
              </p>
            </div>

            <form onSubmit={handleSubmit} className="mt-8 space-y-5">
              <div className="space-y-2">
                <Label htmlFor="email" className="text-sm text-slate-600">
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
                <Label htmlFor="password" className="text-sm text-slate-600">
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
                    className="absolute right-3 top-1/2 -translate-y-1/2 rounded-full p-1 text-slate-400 transition hover:text-slate-600"
                  >
                    {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                  </button>
                </div>
              </div>

              <div className="flex items-center justify-between text-sm text-slate-500">
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
                  className="text-blue-500 hover:text-blue-600 transition"
                >
                  {t("login.forgotPassword")}
                </button>
              </div>

              {error && (
                <div className="text-center text-sm text-rose-500">
                  {error}
                </div>
              )}

              <Button
                type="submit"
                className="h-12 w-full rounded-xl bg-[#3b6cff] text-base font-medium text-white shadow-[0_8px_24px_rgba(59,108,255,0.25)] transition hover:bg-[#325be6]"
                disabled={isLoading}
              >
                {isLoading ? t("login.loggingIn") : t("login.loginButton")}
              </Button>

              <div className="text-center text-xs text-slate-400 pt-2">
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
          --ink: #1e293b;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
          color: var(--ink);
        }

        .login-background {
          position: absolute;
          inset: 0;
          background: linear-gradient(180deg, #f8fafc 0%, #f1f5f9 50%, #e2e8f0 100%);
        }

        .login-blob {
          position: absolute;
          border-radius: 999px;
          filter: blur(60px);
          opacity: 0.5;
        }

        .blob-left {
          width: 400px;
          height: 400px;
          top: -150px;
          left: -150px;
          background: radial-gradient(circle at 30% 30%, #93c5fd, transparent 70%);
        }

        .blob-right {
          width: 450px;
          height: 450px;
          bottom: -180px;
          right: -180px;
          background: radial-gradient(circle at 30% 30%, #a5b4fc, transparent 70%);
        }

        .login-grid {
          position: absolute;
          inset: 0;
          background-image:
            linear-gradient(135deg, rgba(255, 255, 255, 0.5) 0%, rgba(255, 255, 255, 0) 60%);
          opacity: 0.6;
          pointer-events: none;
        }

        .brand-chip {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          padding: 6px 18px;
          border-radius: 999px;
          background: rgba(255, 255, 255, 0.85);
          border: 1px solid #e2e8f0;
          color: #475569;
          font-size: 12px;
          letter-spacing: 0.25em;
          text-transform: uppercase;
          font-weight: 500;
        }

        .login-title {
          letter-spacing: 0.12em;
        }

        .login-input {
          height: 48px;
          border-radius: 10px;
          border: 1px solid #e2e8f0;
          background: #fff;
          font-size: 15px;
          padding-left: 14px;
          color: #1e293b;
          transition: all 0.2s;
        }

        .login-input:focus-visible {
          border-color: #3b82f6;
          box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
          outline: none;
        }

        .email-suffix {
          position: absolute;
          right: 14px;
          top: 50%;
          transform: translateY(-50%);
          font-weight: 500;
          color: #64748b;
          font-size: 14px;
          pointer-events: none;
        }

        .corner-fold {
          position: absolute;
          top: 0;
          right: 0;
          width: 90px;
          height: 90px;
          background: linear-gradient(145deg, rgba(147, 197, 253, 0.4), rgba(255, 255, 255, 0.6));
          clip-path: polygon(35% 0, 100% 0, 100% 65%);
        }

        .corner-fold::after {
          content: "";
          position: absolute;
          inset: 14px 8px auto auto;
          width: 50px;
          height: 50px;
          clip-path: polygon(35% 0, 100% 0, 100% 65%);
          background-image:
            linear-gradient(45deg, rgba(59, 130, 246, 0.4) 25%, transparent 25%),
            linear-gradient(-45deg, rgba(59, 130, 246, 0.4) 25%, transparent 25%),
            linear-gradient(45deg, transparent 75%, rgba(59, 130, 246, 0.4) 75%),
            linear-gradient(-45deg, transparent 75%, rgba(59, 130, 246, 0.4) 75%);
          background-size: 8px 8px;
          background-position: 0 0, 0 4px, 4px -4px, -4px 0px;
          opacity: 0.6;
        }
      `}</style>
    </div>
  );
}
