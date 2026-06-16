import { lazy, Suspense, type ComponentType } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppLayout } from "@/layouts/AppLayout";
import { ProtectedRoute, ForbiddenPage } from "@/components/ProtectedRoute";

function lazyNamed<TModule, TKey extends keyof TModule>(
  loader: () => Promise<TModule>,
  exportName: TKey
) {
  return lazy(async () => {
    const module = await loader();
    return { default: module[exportName] as ComponentType<object> };
  });
}

const LoginPage = lazyNamed(() => import("@/pages/Login"), "LoginPage");
const SharePage = lazyNamed(() => import("@/pages/SharePage"), "SharePage");
const QuizPage = lazyNamed(() => import("@/pages/QuizPage"), "QuizPage");
const EnterpriseDashboard = lazyNamed(
  () => import("@/pages/dashboard/index"),
  "EnterpriseDashboard"
);
const ServicesPage = lazyNamed(() => import("@/pages/Services"), "ServicesPage");
const PlaygroundPage = lazyNamed(() => import("@/pages/playground"), "PlaygroundPage");
const TasksPage = lazyNamed(() => import("@/pages/tasks"), "TasksPage");
const SettingsPage = lazyNamed(() => import("@/pages/Settings"), "SettingsPage");
const UserManagementPage = lazyNamed(() => import("@/pages/UserManagement"), "UserManagementPage");
const UserEditPage = lazyNamed(() => import("@/pages/UserEdit"), "UserEditPage");
const KnowledgeDatasetsPage = lazyNamed(() => import("@/pages/knowledge"), "KnowledgeDatasetsPage");
const KnowledgeDatasetDetailPage = lazyNamed(() => import("@/pages/knowledge"), "KnowledgeDatasetDetailPage");
const DatasetCreatePage = lazy(() => import("@/pages/knowledge/DatasetCreate"));
const AssistantPage = lazyNamed(() => import("@/pages/assistant"), "AssistantPage");
const ExamsPage = lazyNamed(() => import("@/pages/exams"), "ExamsPage");
const ExamDetailPage = lazyNamed(() => import("@/pages/exams/ExamDetailPage"), "ExamDetailPage");

function RouteFallback() {
  return (
    <div className="flex min-h-[240px] items-center justify-center text-sm text-muted-foreground">
      Loading...
    </div>
  );
}

export function AppRouter() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
        {/* Public routes */}
        <Route path="/login" element={<LoginPage />} />
        <Route path="/403" element={<ForbiddenPage />} />
        <Route path="/share/:shareId" element={<SharePage />} />
        <Route path="/quiz/:shareCode" element={<QuizPage />} />

        {/* Protected routes - wrapped with single ProtectedRoute at layout level */}
        <Route
          element={
            <ProtectedRoute>
              <AppLayout />
            </ProtectedRoute>
          }
        >
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route
            path="/dashboard"
            element={
              <ProtectedRoute requiredPermission="console:dashboard:view">
                <EnterpriseDashboard />
              </ProtectedRoute>
            }
          />
          <Route
            path="/services"
            element={
              <ProtectedRoute requiredPermission="console:services:view">
                <ServicesPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/knowledge"
            element={
              <ProtectedRoute requiredPermission="knowledge:dataset:view">
                <KnowledgeDatasetsPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/knowledge/create"
            element={
              <ProtectedRoute requiredPermission="knowledge:dataset:create">
                <DatasetCreatePage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/knowledge/:datasetId"
            element={
              <ProtectedRoute requiredPermission="knowledge:dataset:view">
                <KnowledgeDatasetDetailPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/playground"
            element={
              <ProtectedRoute requiredPermission="conversation:playground:access">
                <PlaygroundPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/assistant"
            element={
              <ProtectedRoute
                requiredPermission="conversation:playground:access"
                blockOnlyRole="model_tester"
                blockRedirectTo="/playground"
              >
                <AssistantPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/tasks"
            element={
              <ProtectedRoute requiredPermission="console:dashboard:view">
                <TasksPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/settings"
            element={
              <ProtectedRoute requiredPermission="console:settings:view">
                <SettingsPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/exams"
            element={
              <ProtectedRoute requiredPermission="console:dashboard:view">
                <ExamsPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/exams/:examId"
            element={
              <ProtectedRoute requiredPermission="console:dashboard:view">
                <ExamDetailPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/users"
            element={
              <ProtectedRoute requiredPermission="user:list">
                <UserManagementPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/users/:userId/edit"
            element={
              <ProtectedRoute requiredPermission="user:edit">
                <UserEditPage />
              </ProtectedRoute>
            }
          />
        </Route>
      </Routes>
    </Suspense>
  );
}
