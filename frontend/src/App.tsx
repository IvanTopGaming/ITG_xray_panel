import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ToastContainer, Slide } from 'react-toastify';
import 'react-toastify/dist/ReactToastify.css';

import { useAuthStore, AuthState } from '@/stores/authStore';
import { panelBase } from '@/lib/panelBase';
import { isWorker } from '@/lib/panelRole';
import { Layout } from '@/components/layout/Layout';
import Login from '@/pages/Login';
import Dashboard from '@/pages/Dashboard';
import Routing from '@/pages/Routing';
import System from '@/pages/System';
import Statistics from '@/pages/Statistics';
import Panels from '@/pages/Panels';
import Bot from '@/pages/Bot';

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
              path="statistics"
              element={isWorker ? <Navigate to="/" replace /> : <Statistics />}
            />
            <Route path="panels" element={isWorker ? <Navigate to="/" replace /> : <Panels />} />
            <Route path="routing" element={<Routing />} />
            <Route path="bot" element={isWorker ? <Navigate to="/" replace /> : <Bot />} />
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
