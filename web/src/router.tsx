import { Navigate, Route, Routes } from "react-router-dom";

import { AppLayout } from "@/layouts/AppLayout";
import { DashboardPage } from "@/pages/Dashboard";
import { ServicesPage } from "@/pages/Services";
import { PlaygroundPage } from "@/pages/Playground";
import { TasksPage } from "@/pages/Tasks";
import { SettingsPage } from "@/pages/Settings";
import { LoginPage } from "@/pages/Login";
import { UserManagementPage } from "@/pages/UserManagement";
import { UserEditPage } from "@/pages/UserEdit";
import { KnowledgeDatasetsPage, KnowledgeDatasetDetailPage } from "@/pages/knowledge";
import DatasetCreatePage from "@/pages/knowledge/DatasetCreate";
import { AssistantPage } from "@/pages/assistant";
import {
  ConnectionListPage,
  ConnectionCreatePage,
  BindSpacePage,
  SyncedPagesPage,
} from "@/pages/confluence";
import { MonitoringDashboard } from "@/pages/monitoring";
import { ProtectedRoute, ForbiddenPage } from "@/components/ProtectedRoute";

export function AppRouter() {
  return (
    <Routes>
      {/* Public routes */}
      <Route path="/login" element={<LoginPage />} />
      <Route path="/403" element={<ForbiddenPage />} />

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
              <DashboardPage />
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
        {/* Confluence Routes - Multi-page architecture */}
        <Route
          path="/confluence"
          element={
            <ProtectedRoute requiredPermission="knowledge:confluence:manage">
              <ConnectionListPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/confluence/connections/new"
          element={
            <ProtectedRoute requiredPermission="knowledge:confluence:manage">
              <ConnectionCreatePage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/confluence/connections/:connectionId/bind"
          element={
            <ProtectedRoute requiredPermission="knowledge:confluence:manage">
              <BindSpacePage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/confluence/bindings/:bindingId/pages"
          element={
            <ProtectedRoute requiredPermission="knowledge:confluence:manage">
              <SyncedPagesPage />
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
            <ProtectedRoute requiredPermission="conversation:playground:access">
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
          path="/monitoring"
          element={
            <ProtectedRoute requiredPermission="console:dashboard:view">
              <MonitoringDashboard />
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
  );
}
