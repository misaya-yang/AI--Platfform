import { Navigate, useLocation } from "react-router-dom";
import { useAuthStore } from "@/store/useAuthStore";

interface ProtectedRouteProps {
  children: React.ReactNode;
  requiredPermission?: string;
  requiredPermissions?: string[];
  requireAll?: boolean; // if true, require all permissions; if false (default), require any
}

export function ProtectedRoute({
  children,
  requiredPermission,
  requiredPermissions = [],
  requireAll = false,
}: ProtectedRouteProps) {
  const location = useLocation();
  const hydrated = useAuthStore((state) => state.hydrated);
  const isAuthenticated = useAuthStore((state) => state.isAuthenticated);
  const sessionValidation = useAuthStore((state) => state.sessionValidation);
  const hasAnyPermission = useAuthStore((state) => state.hasAnyPermission);
  const hasAllPermissions = useAuthStore((state) => state.hasAllPermissions);

  // Show loading while waiting for hydration
  if (!hydrated || sessionValidation === "checking") {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="text-gray-500">Loading...</div>
      </div>
    );
  }

  // Check if user is authenticated
  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  // Check permissions
  const allRequiredPermissions = requiredPermission
    ? [requiredPermission, ...requiredPermissions]
    : requiredPermissions;

  if (allRequiredPermissions.length > 0) {
    const hasAccess = requireAll
      ? hasAllPermissions(allRequiredPermissions)
      : hasAnyPermission(allRequiredPermissions);

    if (!hasAccess) {
      return <Navigate to="/403" replace />;
    }
  }

  return <>{children}</>;
}

// Forbidden page component
export function ForbiddenPage() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-background">
      <div className="text-center space-y-4">
        <h1 className="text-6xl font-bold text-muted-foreground">403</h1>
        <h2 className="text-2xl font-semibold">Access Denied</h2>
        <p className="text-muted-foreground">
          You don't have permission to access this page.
        </p>
        <a
          href="/"
          className="inline-block px-4 py-2 bg-primary text-primary-foreground rounded-md hover:bg-primary/90"
        >
          Go to Dashboard
        </a>
      </div>
    </div>
  );
}
