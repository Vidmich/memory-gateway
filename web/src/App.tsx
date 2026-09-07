import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import { ApiError } from '@/api/client'
import { AuthProvider } from '@/auth/AuthContext'
import { ProtectedRoute } from '@/auth/ProtectedRoute'
import { ToastProvider } from '@/components/Toast'
import { AppShell } from '@/layout/AppShell'
import { AcceptInvitationPage } from '@/pages/AcceptInvitationPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { GatewayFormPage } from '@/pages/GatewayFormPage'
import { GatewaysPage } from '@/pages/GatewaysPage'
import { LoginPage } from '@/pages/LoginPage'
import { MembersPage } from '@/pages/MembersPage'
import { ModelFormPage } from '@/pages/ModelFormPage'
import { ModelsPage } from '@/pages/ModelsPage'
import { NotFoundPage } from '@/pages/NotFoundPage'
import { OrganizationSettingsPage } from '@/pages/OrganizationSettingsPage'
import { OrganizationsPage } from '@/pages/OrganizationsPage'

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        retry: (failureCount, error) => {
          // Retrying a 4xx just repeats the same mistake, and a 401 has already been
          // through the client's refresh-and-retry by the time it surfaces here.
          if (error instanceof ApiError && error.status < 500) return false
          return failureCount < 2
        },
      },
    },
  })
}

/** Split out from `App` so tests can mount the routes with their own providers. */
export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      {/* Public: whoever follows an invitation link has no account yet. */}
      <Route path="/invitations/accept/:token" element={<AcceptInvitationPage />} />
      <Route
        element={
          <ProtectedRoute>
            <AppShell />
          </ProtectedRoute>
        }
      >
        <Route path="/" element={<DashboardPage />} />
        <Route path="/models" element={<ModelsPage />} />
        {/* One component for both: creating and editing differ by whether there is an
            id, not by which fields exist. */}
        <Route path="/models/new" element={<ModelFormPage />} />
        <Route path="/models/:modelId" element={<ModelFormPage />} />
        <Route path="/gateways" element={<GatewaysPage />} />
        {/* `?clone=<id>` pre-fills from an existing gateway — the escape hatch for the
            immutable slug. */}
        <Route path="/gateways/new" element={<GatewayFormPage />} />
        <Route path="/gateways/:gatewayId" element={<GatewayFormPage />} />
        <Route path="/settings" element={<OrganizationSettingsPage />} />
        <Route path="/settings/members" element={<MembersPage />} />
        {/* Rendering is gated by capability; the API is what actually refuses. */}
        <Route path="/platform/organizations" element={<OrganizationsPage />} />
      </Route>
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}

export function App() {
  return (
    <QueryClientProvider client={makeQueryClient()}>
      <AuthProvider>
        <ToastProvider>
          <BrowserRouter>
            <AppRoutes />
          </BrowserRouter>
        </ToastProvider>
      </AuthProvider>
    </QueryClientProvider>
  )
}
