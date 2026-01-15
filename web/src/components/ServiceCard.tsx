import { useState } from "react";
import type { HealthStatus, ServiceDefinition, ServiceType } from "@/types/gateway";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ServiceConfigDialog } from "@/components/ServiceConfigDialog";
import { cn } from "@/lib/utils";
import {
  MessageSquare,
  Sparkles,
  Cog,
  BarChart3,
  Tag,
  GitBranch,
  Wrench,
  Package,
  Settings,
} from "lucide-react";

// Simple icon components for service types
function ServiceIcon({ serviceType }: { serviceType: ServiceType }) {
  const iconProps = { className: "h-6 w-6" };

  switch (serviceType) {
    case "conversational":
      return <MessageSquare {...iconProps} />;
    case "generative":
      return <Sparkles {...iconProps} />;
    case "processing":
      return <Cog {...iconProps} />;
    case "embedding":
      return <BarChart3 {...iconProps} />;
    case "classification":
      return <Tag {...iconProps} />;
    case "langgraph":
      return <GitBranch {...iconProps} />;
    case "custom":
      return <Wrench {...iconProps} />;
    default:
      return <Package {...iconProps} />;
  }
}

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
      <Card
        className={cn(
          "relative overflow-hidden cursor-pointer",
          selected && "ring-2 ring-primary"
        )}
        onClick={onSelect}
      >
        <CardContent className="p-5">
          <div className="flex items-start gap-4">
            {/* Simple icon - no gradient container */}
            <div className="text-muted-foreground">
              <ServiceIcon serviceType={service.service_type} />
            </div>

            <div className="flex-1 min-w-0">
              <h3 className="font-medium text-foreground truncate">
                {service.name}
              </h3>
              <p className="text-sm text-muted-foreground mt-1">
                {service.service_id}
              </p>
              {service.description && (
                <p className="text-xs text-muted-foreground mt-2 line-clamp-2">
                  {service.description}
                </p>
              )}
            </div>

            <div className="flex items-center gap-2">
              {/* Status badge */}
              <Badge variant={isHealthy ? "default" : "destructive"}>
                {isHealthy ? "Active" : "Inactive"}
              </Badge>

              {/* Config button */}
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8 text-muted-foreground"
                onClick={(e) => {
                  e.stopPropagation();
                  setConfigOpen(true);
                }}
                title="Config"
              >
                <Settings className="h-4 w-4" />
              </Button>
            </div>
          </div>

          {/* Service type and modes badges */}
          <div className="flex flex-wrap gap-2 mt-4">
            <Badge variant="secondary" className="text-xs">
              {service.service_type}
            </Badge>
            {service.supported_modes?.slice(0, 2).map((m) => (
              <Badge key={m} variant="outline" className="text-xs">
                {m}
              </Badge>
            ))}
          </div>
        </CardContent>
      </Card>

      <ServiceConfigDialog
        serviceId={service.service_id}
        serviceName={service.name}
        open={configOpen}
        onOpenChange={setConfigOpen}
      />
    </>
  );
}
