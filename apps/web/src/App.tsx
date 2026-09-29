import { Route, Routes } from "react-router-dom";

import { ProtectedRoute } from "./auth/ProtectedRoute";
import { AppShell } from "./components/AppShell";
import { AboutPage } from "./pages/AboutPage";
import { AccountRecoveryPage } from "./pages/AccountRecoveryPage";
import { AuthPage } from "./pages/AuthPage";
import { HomePage } from "./pages/HomePage";
import { JobDetailPage } from "./pages/JobDetailPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { WorkspacePage } from "./pages/WorkspacePage";

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<HomePage />} />
        <Route path="about" element={<AboutPage />} />
        <Route element={<ProtectedRoute />}>
          <Route path="workspace" element={<WorkspacePage />} />
          <Route path="workspace/jobs/:jobId" element={<JobDetailPage />} />
        </Route>
        <Route path="*" element={<NotFoundPage />} />
      </Route>
      <Route path="auth/verify-email" element={<AccountRecoveryPage mode="verify-email" />} />
      <Route path="auth/forgot-password" element={<AccountRecoveryPage mode="forgot-password" />} />
      <Route path="auth/reset-password" element={<AccountRecoveryPage mode="reset-password" />} />
      <Route path="auth/:mode" element={<AuthPage />} />
    </Routes>
  );
}
