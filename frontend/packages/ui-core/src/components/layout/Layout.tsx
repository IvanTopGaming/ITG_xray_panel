import { useState } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { Menu } from 'lucide-react';
import { Sidebar } from './Sidebar';
import { useAuthStore, AuthState } from '@/stores/authStore';
import { AnimatedBackground } from './AnimatedBackground';
import { DisplayConfigLoader } from '@/components/DisplayConfigLoader';
import { motion, AnimatePresence } from 'framer-motion';

export function Layout() {
  const [isSidebarOpen, setSidebarOpen] = useState(false);
  const user = useAuthStore((state: AuthState) => state.username);
  const location = useLocation();

  const getTitle = () => {
    switch (location.pathname) {
      case '/':
        return 'Dashboard';
      case '/routing':
        return 'Routing';
      case '/system':
        return 'System';
      default:
        return 'Panel';
    }
  };

  return (
    <div className="flex h-screen w-full text-gray-100 overflow-hidden relative font-sans">
      <AnimatedBackground />
      <Sidebar isOpen={isSidebarOpen} onClose={() => setSidebarOpen(false)} />

      <main className="flex-1 flex flex-col h-full min-w-0 relative z-10">
        <header className="h-20 px-6 md:px-10 flex items-center justify-between shrink-0 border-b border-white/[0.04] bg-gradient-to-b from-[#080610]/60 to-transparent backdrop-blur-sm">
          <div className="flex items-center gap-4">
            <button
              className="md:hidden p-2 -ml-2 text-gray-400 hover:text-white transition-colors"
              onClick={() => setSidebarOpen(true)}
            >
              <Menu size={24} />
            </button>
            <AnimatePresence mode="wait">
              <motion.div
                key={location.pathname}
                initial={{ opacity: 0, x: -12 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 12 }}
                transition={{ duration: 0.18, ease: 'easeOut' }}
              >
                <h1 className="text-2xl font-bold text-white tracking-tight">{getTitle()}</h1>
                <p className="text-[10px] text-primary/50 font-semibold tracking-[0.2em] uppercase mt-0.5">
                  Xray Control Panel
                </p>
              </motion.div>
            </AnimatePresence>
          </div>

          <motion.div
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ duration: 0.2 }}
            className="flex items-center gap-3"
          >
            <div className="flex items-center gap-3 bg-white/[0.05] backdrop-blur-md px-4 py-2 rounded-full border border-white/[0.08]">
              <div className="text-right hidden sm:block">
                <div className="text-sm font-semibold text-gray-200 leading-none">
                  {user || 'Admin'}
                </div>
              </div>
              <div className="w-8 h-8 rounded-full bg-gradient-to-br from-primary to-[#4f378b] flex items-center justify-center text-white font-bold shadow-[0_0_12px_rgba(208,188,255,0.25)] text-sm">
                {(user?.[0] || 'A').toUpperCase()}
              </div>
            </div>
          </motion.div>
        </header>

        <div className="flex-1 overflow-y-auto px-4 md:px-10 pb-10 custom-scrollbar">
          <div className="max-w-7xl mx-auto w-full pt-6">
            <DisplayConfigLoader />
            <Outlet />
          </div>
        </div>
      </main>
    </div>
  );
}
