import { Navigate, Route, Routes } from "react-router-dom";

import { AppLayout } from "@/layouts/AppLayout";
import { DashboardPage } from "@/pages/Dashboard";
import { ServicesPage } from "@/pages/Services";
import { PlaygroundPage } from "@/pages/Playground";
import { TasksPage } from "@/pages/Tasks";
import { SettingsPage } from "@/pages/Settings";
import { KnowledgeDatasetsPage, KnowledgeDatasetDetailPage } from "@/pages/knowledge";
import DatasetCreatePage from "@/pages/knowledge/DatasetCreate";
import ConfluencePage from "@/pages/confluence/ConfluencePage";

export function AppRouter() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="/services" element={<ServicesPage />} />
        <Route path="/knowledge" element={<KnowledgeDatasetsPage />} />
        <Route path="/knowledge/create" element={<DatasetCreatePage />} />
        <Route path="/knowledge/:datasetId" element={<KnowledgeDatasetDetailPage />} />
        <Route path="/confluence" element={<ConfluencePage />} />
        <Route path="/playground" element={<PlaygroundPage />} />
        <Route path="/tasks" element={<TasksPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
    </Routes>
  );
}
