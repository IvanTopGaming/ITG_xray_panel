import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastContainer, Slide } from 'react-toastify';
import 'react-toastify/dist/ReactToastify.css';

import { useAuthStore, AuthState } from '@ui/stores/authStore';
import { panelBase } from '@ui/lib/panelBase';
import { hasLocalXray } from '@ui/lib/panelRole';
import { Layout } from '@ui/components/layout/Layout';
import Login from '@ui/pages/Login';
import Dashboard from '@ui/pages/Dashboard';
import Routing from '@ui/pages/Routing';
import System from '@ui/pages/System';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

function ProtectedRoute({ children }: { children: JSX.Element }) {
  const isAuthenticated = useAuthStore((state: AuthState) => state.isAuthenticated());
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return children;
}

const basename = panelBase;

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename={basename}>
        <Routes>
          <Route path="/login" element={<Login />} />

          <Route
            path="/"
            element={
              <ProtectedRoute>
                <Layout />
              </ProtectedRoute>
            }
          >
            <Route index element={<Dashboard />} />
            <Route
              path="routing"
              element={hasLocalXray ? <Routing /> : <Navigate to="/" replace />}
            />
            <Route path="system" element={<System />} />
          </Route>
        </Routes>
      </BrowserRouter>

      <ToastContainer
        position="top-right"
        autoClose={3000}
        hideProgressBar={false}
        newestOnTop={true}
        closeOnClick
        rtl={false}
        pauseOnFocusLoss
        draggable
        pauseOnHover
        theme="dark"
        transition={Slide}
        toastClassName="glass-toast"
        bodyClassName="glass-toast-body"
        progressClassName="glass-toast-progress"
      />
    </QueryClientProvider>
  );
}

export default App;
