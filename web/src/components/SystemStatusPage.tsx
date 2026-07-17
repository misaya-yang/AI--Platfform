import { ArrowRight, SearchX, ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { Logo } from "@/components/Logo";
import { Button } from "@/components/ui/button";
import { useAuthStore } from "@/store/useAuthStore";

type SystemStatusKind = "forbidden" | "notFound";

interface SystemStatusPageProps {
  kind: SystemStatusKind;
}

const statusConfig = {
  forbidden: {
    code: "403",
    icon: ShieldAlert,
    titleKey: "systemPages.forbidden.title",
    descriptionKey: "systemPages.forbidden.description",
  },
  notFound: {
    code: "404",
    icon: SearchX,
    titleKey: "systemPages.notFound.title",
    descriptionKey: "systemPages.notFound.description",
  },
} satisfies Record<
  SystemStatusKind,
  {
    code: string;
    icon: typeof ShieldAlert;
    titleKey: string;
    descriptionKey: string;
  }
>;

export function SystemStatusPage({ kind }: SystemStatusPageProps) {
  const { t } = useTranslation();
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const hasPermission = useAuthStore((state) => state.hasPermission);
  const config = statusConfig[kind];
  const Icon = config.icon;

  const destination = !isAuthenticated
    ? "/login"
    : hasPermission("console:dashboard:view")
      ? "/dashboard"
      : hasPermission("conversation:playground:access")
        ? "/playground"
        : "/login";

  return (
    <main className="relative flex min-h-dvh items-center justify-center overflow-hidden bg-background px-6 py-16">
      <div className="absolute left-6 top-6 sm:left-10 sm:top-8">
        <Logo collapsed={false} />
      </div>

      <section className="w-full max-w-lg text-center" aria-labelledby={`${kind}-title`}>
        <div className="mx-auto mb-6 flex h-12 w-12 items-center justify-center rounded-xl border border-border bg-muted/40 text-muted-foreground">
          <Icon aria-hidden="true" size={22} strokeWidth={1.6} />
        </div>
        <p className="mb-3 text-xs font-medium uppercase tracking-[0.18em] text-muted-foreground">
          {t("systemPages.eyebrow")}
        </p>
        <h1 className="mb-2 font-mono text-sm font-normal text-primary">{config.code}</h1>
        <h2 id={`${kind}-title`} className="text-3xl font-semibold tracking-tight text-foreground sm:text-4xl">
          {t(config.titleKey)}
        </h2>
        <p className="mx-auto mt-4 max-w-md text-sm leading-6 text-muted-foreground sm:text-base">
          {t(config.descriptionKey)}
        </p>
        <Button asChild className="mt-8 h-10 px-5">
          <Link to={destination}>
            {t("systemPages.returnToWorkspace")}
            <ArrowRight aria-hidden="true" className="ml-2 h-4 w-4" />
          </Link>
        </Button>
      </section>
    </main>
  );
}

export function ForbiddenPage() {
  return <SystemStatusPage kind="forbidden" />;
}

export function NotFoundPage() {
  return <SystemStatusPage kind="notFound" />;
}
