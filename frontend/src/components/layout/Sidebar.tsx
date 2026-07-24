import { useLocation, useNavigate } from 'react-router-dom';
import {
  LayoutDashboard,
  Route as RouteIcon,
  Settings,
  LogOut,
  Radar,
  BarChart3,
  Server,
  Bot as BotIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useAuthStore, AuthState } from '@/stores/authStore';
import { motion } from 'framer-motion';
import { useVersionStatus } from '@/hooks/useVersionStatus';
import { isWorker } from '@/lib/panelRole';

export function Sidebar({ isOpen, onClose }: { isOpen: boolean; onClose: () => void }) {
  const location = useLocation();
  const navigate = useNavigate();
  const logout = useAuthStore((state: AuthState) => state.logout);
  const { hasUpdates } = useVersionStatus();

  const WORKER_HIDDEN = new Set(['/statistics', '/panels', '/bot']);
  const navItems = [
    { icon: LayoutDashboard, label: 'Panel', path: '/' },
    { icon: BarChart3, label: 'Stats', path: '/statistics' },
    { icon: Server, label: 'Panels', path: '/panels' },
    { icon: RouteIcon, label: 'Routing', path: '/routing' },
    { icon: BotIcon, label: 'Bot', path: '/bot' },
    { icon: Settings, label: 'System', path: '/system' },
  ].filter((item) => !(isWorker && WORKER_HIDDEN.has(item.path)));

  const handleNav = (path: string) => {
    navigate(path);
    if (window.innerWidth < 768) onClose();
  };

  return (
    <>
      <div
        className={cn(
          'fixed inset-0 bg-black/60 z-30 md:hidden backdrop-blur-sm transition-opacity',
          isOpen ? 'opacity-100' : 'opacity-0 pointer-events-none'
        )}
        onClick={onClose}
      />

      <aside
        className={cn(
          'fixed md:static inset-y-0 left-0 z-40 w-[96px] bg-[#120f17]/90 border-r border-white/5 flex flex-col items-center py-8 transition-transform duration-300 md:translate-x-0 backdrop-blur-2xl shadow-2xl',
          isOpen ? 'translate-x-0' : '-translate-x-full'
        )}
      >
        <motion.div
          whileHover={{ rotate: 180, scale: 1.1 }}
          transition={{ duration: 0.5 }}
          className="mb-12 text-primary p-3 bg-primary/10 rounded-2xl cursor-pointer"
        >
          <Radar size={32} />
        </motion.div>

        <nav className="flex-1 flex flex-col gap-6 w-full px-4">
          {navItems.map((item) => {
            const isActive = location.pathname === item.path;
            return (
              <div key={item.path} className="relative group">
                {isActive && (
                  <motion.div
                    layoutId="activeTab"
                    className="absolute inset-0 bg-gradient-to-br from-primary/20 to-secondary/20 rounded-2xl border border-white/10 shadow-[0_0_20px_rgba(208,188,255,0.2)]"
                    initial={false}
                    transition={{ type: 'spring', stiffness: 500, damping: 30 }}
                  />
                )}

                <button
                  onClick={() => handleNav(item.path)}
                  className={cn(
                    'relative z-10 flex flex-col items-center justify-center w-full h-[72px] rounded-2xl transition-all duration-200',
                    isActive ? 'text-white' : 'text-gray-500 hover:text-gray-300'
                  )}
                >
                  <item.icon
                    size={26}
                    className={cn(
                      'mb-1.5 transition-transform duration-300',
                      isActive ? 'scale-110' : 'group-hover:scale-110'
                    )}
                  />
                  <span className="text-[10px] font-bold tracking-wide">{item.label}</span>
                </button>

                {hasUpdates && item.path === '/system' && (
                  <span
                    title="Update available"
                    className="absolute top-2 right-2 z-20 h-2.5 w-2.5 rounded-full bg-primary shadow-[0_0_8px_rgba(208,188,255,0.8)]"
                  />
                )}
              </div>
            );
          })}
        </nav>

        <motion.button
          whileHover={{ scale: 1.1, backgroundColor: 'rgba(242, 184, 181, 0.1)' }}
          whileTap={{ scale: 0.9 }}
          onClick={logout}
          className="mt-auto w-12 h-12 flex items-center justify-center text-gray-500 hover:text-error rounded-2xl transition-colors mb-4"
          title="Logout"
        >
          <LogOut size={24} />
        </motion.button>
      </aside>
    </>
  );
}
