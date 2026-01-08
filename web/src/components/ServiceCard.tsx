import { useState } from "react";
import { motion } from "framer-motion";
import type { HealthStatus, ServiceDefinition } from "@/types/gateway";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
// import { HealthBadge } from "@/components/HealthBadge"; // Using custom indicator
import { ServiceConfigDialog } from "@/components/ServiceConfigDialog";
import { cn } from "@/lib/utils";

export function ServiceCard({
  service,
  health,
  onSelect,
  selected,
}: {
  service: ServiceDefinition;
  health?: HealthStatus;
  selected?: boolean;
  onSelect?: () => void;
}) {
  const [configOpen, setConfigOpen] = useState(false);
  const isHealthy = health?.status === "healthy";

  return (
    <>
      <motion.div
        whileHover={{ scale: 1.02 }}
        whileTap={{ scale: 0.98 }}
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.2 }}
      >
        <Card
          className={cn(
            "relative overflow-hidden cursor-pointer transition-all duration-300 border-0",
            selected
              ? "shadow-[0_0_0_2px_hsl(var(--primary)),0_4px_20px_rgba(59,130,246,0.25)] bg-primary/5"
              : "shadow-lg bg-card hover:bg-accent/5 hover:shadow-xl"
          )}
          onClick={onSelect}
        >
          {/* Decorative gradient orb */}
          <div className="absolute -right-4 -top-4 w-24 h-24 bg-gradient-to-br from-primary/15 to-primary/5 rounded-full blur-2xl pointer-events-none" />

          <CardHeader className="pb-3 relative z-10">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2.5">
                <CardTitle className="text-lg font-semibold tracking-tight">{service.name}</CardTitle>
                {/* Custom Health Indicator with pulse animation */}
                <div
                  className={cn(
                    "relative flex h-3 w-3 items-center justify-center rounded-full",
                    isHealthy ? "bg-green-500/20" : "bg-red-500/20"
                  )}
                  title={isHealthy ? "Healthy" : "Unhealthy"}
                >
                  <div className={cn(
                    "h-2 w-2 rounded-full",
                    isHealthy ? "bg-green-500" : "bg-red-500",
                    isHealthy && "status-pulse"
                  )} />
                </div>
              </div>

              <div className="flex items-center gap-1 z-20">
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-8 w-8 text-muted-foreground hover:text-primary"
                  onClick={(e) => {
                    e.stopPropagation();
                    setConfigOpen(true);
                  }}
                  title="Config"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.1a2 2 0 0 1-1-1.72v-.51a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" /><circle cx="12" cy="12" r="3" /></svg>
                </Button>
              </div>
            </div>
            <CardDescription className="line-clamp-2 text-xs leading-relaxed opacity-80">
              {service.description || "No description provided."}
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2 relative z-10">
            <Badge variant="secondary" className="bg-primary/10 backdrop-blur-sm text-[10px] px-2.5 py-0.5 font-medium uppercase tracking-wider text-primary/80 border-0 rounded-md">
              {service.service_type}
            </Badge>
            {service.supported_modes?.slice(0, 2).map((m) => (
              <Badge key={m} variant="secondary" className="bg-secondary/60 backdrop-blur-sm text-[10px] px-2.5 py-0.5 font-normal text-muted-foreground border-0 rounded-md">
                {m}
              </Badge>
            ))}
          </CardContent>
        </Card>
      </motion.div>

      <ServiceConfigDialog
        serviceId={service.service_id}
        serviceName={service.name}
        open={configOpen}
        onOpenChange={setConfigOpen}
      />
    </>
  );
}
