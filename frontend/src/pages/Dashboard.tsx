import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import api from '@/lib/api';
import {
  Inbound,
  SystemStats,
  Client,
  ClientDevice,
  Outbound,
  Balancer,
  Node,
  MasterInfo,
} from '@/lib/types';
import { formatBytes, cn } from '@/lib/utils';
import { generateLink, generateSubscriptionUrl } from '@/lib/protocols';
import { deviceIcon, timeAgo } from '@/lib/devices';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { ConfirmationModal } from '@/components/ui/ConfirmationModal';
import { Input } from '@/components/ui/Input';
import { TagInput } from '@/components/ui/TagInput';
import { InboundForm } from '@/components/inbound/InboundForm';
import { UserForm } from '@/components/inbound/UserForm';
import {
  Plus,
  ArrowUp,
  ArrowDown,
  Activity,
  Cpu,
  Trash2,
  Copy,
  QrCode,
  Edit,
  RotateCcw,
  Zap,
  Network,
  Users,
  Search,
  X,
  Link2,
  Power,
  PowerOff,
  CheckSquare,
  Square,
  Minus,
  Tag,
  ChevronDown,
  Smartphone,
  Loader2,
} from 'lucide-react';
import { toast } from 'react-toastify';
import { QRCodeCanvas } from 'qrcode.react';
import { motion, AnimatePresence, animate, useMotionValue } from 'framer-motion';
import { Select } from '@/components/ui/Select';

// ─── User status ─────────────────────────────────────────────────────────────

type UserStatus = 'online' | 'offline' | 'expired' | 'overlimit' | 'disabled';
type StatusFilter = 'all' | UserStatus;

function getClientStatus(client: Client, now: number): UserStatus {
  if (client.expiry_time > 0 && now > client.expiry_time) return 'expired';
  if (client.limit_bytes > 0 && client.up + client.down >= client.limit_bytes) return 'overlimit';
  if (!client.enable) return 'disabled';
  if (client.last_seen && now - client.last_seen < 5 * 60 * 1000) return 'online';
  return 'offline';
}

const STATUS_FILTERS: Array<{
  key: StatusFilter;
  label: string;
  dotCls: string;
  pillCls: string;
  textCls: string;
}> = [
  {
    key: 'all',
    label: 'All',
    dotCls: '',
    pillCls: 'bg-white/10 border-white/15',
    textCls: 'text-white',
  },
  {
    key: 'online',
    label: 'Online',
    dotCls: 'bg-emerald-400 shadow-[0_0_4px_rgba(52,211,153,0.9)]',
    pillCls: 'bg-emerald-500/15 border-emerald-500/30',
    textCls: 'text-emerald-400',
  },
  {
    key: 'offline',
    label: 'Offline',
    dotCls: 'bg-zinc-500',
    pillCls: 'bg-zinc-500/15 border-zinc-500/30',
    textCls: 'text-zinc-400',
  },
  {
    key: 'expired',
    label: 'Expired',
    dotCls: 'bg-amber-400',
    pillCls: 'bg-amber-500/15 border-amber-500/30',
    textCls: 'text-amber-400',
  },
  {
    key: 'overlimit',
    label: 'Over Limit',
    dotCls: 'bg-red-400',
    pillCls: 'bg-red-500/15 border-red-500/30',
    textCls: 'text-red-400',
  },
  {
    key: 'disabled',
    label: 'Disabled',
    dotCls: 'bg-rose-600',
    pillCls: 'bg-rose-500/15 border-rose-500/30',
    textCls: 'text-rose-400',
  },
];

// Protocol accent colors
const PROTOCOL_COLORS: Record<string, string> = {
  vless: 'bg-violet-500/15 text-violet-300 border-violet-500/25',
  vmess: 'bg-blue-500/15 text-blue-300 border-blue-500/25',
  trojan: 'bg-orange-500/15 text-orange-300 border-orange-500/25',
  shadowsocks: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/25',
  wireguard: 'bg-cyan-500/15 text-cyan-300 border-cyan-500/25',
  socks: 'bg-yellow-500/15 text-yellow-300 border-yellow-500/25',
  http: 'bg-gray-500/15 text-gray-400 border-gray-500/25',
};

const PROTOCOL_GLOW: Record<string, string> = {
  vless: 'from-violet-500/10',
  vmess: 'from-blue-500/10',
  trojan: 'from-orange-500/10',
  shadowsocks: 'from-emerald-500/10',
  wireguard: 'from-cyan-500/10',
  socks: 'from-yellow-500/10',
  http: 'from-gray-500/10',
};

function AnimatedCounter({ value }: { value: number }) {
  const [display, setDisplay] = useState(0);
  const prevRef = useRef(0);

  useEffect(() => {
    const from = prevRef.current;
    const controls = animate(from, value, {
      duration: 0.6,
      ease: [0.25, 0.46, 0.45, 0.94],
      onUpdate: (v) => {
        const rounded = Math.round(v);
        prevRef.current = rounded;
        setDisplay(rounded);
      },
    });
    return () => controls.stop();
  }, [value]);

  return <>{display}</>;
}

function SkeletonCard() {
  return (
    <div className="bg-[#1e1b24]/60 border border-white/5 rounded-3xl overflow-hidden">
      <div className="p-5 md:p-6 border-b border-white/5 flex items-center gap-4">
        <div className="h-12 w-12 rounded-2xl skeleton" />
        <div className="flex-1 space-y-2.5">
          <div className="h-5 w-36 rounded-lg skeleton" />
          <div className="h-3 w-52 rounded-lg skeleton" />
        </div>
        <div className="hidden md:flex gap-2">
          <div className="h-8 w-20 rounded-lg skeleton" />
          <div className="h-8 w-20 rounded-lg skeleton" />
          <div className="h-8 w-10 rounded-lg skeleton" />
        </div>
      </div>
      <div className="p-4 md:p-6 space-y-3">
        {[1, 2].map((i) => (
          <div key={i} className="h-[72px] rounded-xl skeleton" />
        ))}
        <div className="mt-4 pt-4 border-t border-white/5 flex gap-3">
          <div className="h-11 flex-1 rounded-lg skeleton" />
          <div className="h-11 w-28 rounded-lg skeleton" />
        </div>
      </div>
    </div>
  );
}

// Tracks inner content height exactly via ResizeObserver. The container is a
// passive follower — any smoothness comes from animations inside the children
// (e.g. AnimatePresence height transitions). This avoids the "border lags
// behind content" effect of running a spring on top of an already-animating
// child.
function AnimatedHeight({ children }: { children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const heightValue = useMotionValue<number | string>('auto');

  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(() => {
      if (!ref.current) return;
      heightValue.set(ref.current.offsetHeight);
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, [heightValue]);

  return (
    <motion.div style={{ height: heightValue, overflow: 'hidden' }}>
      <div ref={ref}>{children}</div>
    </motion.div>
  );
}

export default function Dashboard() {
  const [stats, setStats] = useState<SystemStats | null>(null);
  const [createModal, setCreateModal] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [searchTerm, setSearchTerm] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const hasShownInboundsRef = useRef(false);
  const [selectedUsers, setSelectedUsers] = useState<Set<string>>(new Set());

  const toggleUser = useCallback((tag: string, email: string) => {
    const key = `${tag}\0${email}`;
    setSelectedUsers((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const clearSelection = useCallback(() => setSelectedUsers(new Set()), []);

  const selectAllInInbound = useCallback((tag: string, emails: string[]) => {
    setSelectedUsers((prev) => {
      const next = new Set(prev);
      emails.forEach((e) => next.add(`${tag}\0${e}`));
      return next;
    });
  }, []);

  const deselectAllInInbound = useCallback((tag: string, emails: string[]) => {
    setSelectedUsers((prev) => {
      const next = new Set(prev);
      emails.forEach((e) => next.delete(`${tag}\0${e}`));
      return next;
    });
  }, []);

  const {
    data: inbounds,
    isLoading,
    isError,
  } = useQuery({
    queryKey: ['inbounds'],
    queryFn: async () => (await api.get<Inbound[]>('/inbounds')).data,
    refetchOnWindowFocus: false,
    refetchInterval: 3000,
  });

  const { data: outbounds } = useQuery({
    queryKey: ['outbounds'],
    queryFn: async () => (await api.get<Outbound[]>('/outbounds')).data,
  });
  const { data: balancers } = useQuery({
    queryKey: ['balancers'],
    queryFn: async () => (await api.get<Balancer[]>('/balancers')).data,
  });

  useEffect(() => {
    const fetchStats = async () => {
      try {
        const sys = await api.get('/stats/system');
        setStats(sys.data);
      } catch (_) {
        /* stats fetch is non-critical */
      }
      setNow(Date.now());
    };
    fetchStats();
    const i = setInterval(fetchStats, 3000);
    return () => clearInterval(i);
  }, []);

  const filteredInbounds = useMemo(() => {
    if (!inbounds) return [];
    const lowerTerm = searchTerm.toLowerCase();
    const supportsUsers = (ib: Inbound) => !['socks', 'http'].includes(ib.protocol);

    return inbounds
      .map((ib) => {
        let clients = ib.settings.clients;

        if (lowerTerm) {
          const tagMatch = ib.tag.toLowerCase().includes(lowerTerm);
          if (!tagMatch) {
            clients = clients.filter(
              (c) => c.email.toLowerCase().includes(lowerTerm) || c.id.includes(lowerTerm)
            );
          }
        }

        if (statusFilter !== 'all' && supportsUsers(ib)) {
          clients = clients.filter((c) => getClientStatus(c, now) === statusFilter);
        }

        if ((lowerTerm || statusFilter !== 'all') && supportsUsers(ib) && clients.length === 0)
          return null;
        if (lowerTerm && !supportsUsers(ib) && !ib.tag.toLowerCase().includes(lowerTerm))
          return null;

        return { ...ib, settings: { ...ib.settings, clients } };
      })
      .filter(Boolean) as Inbound[];
  }, [inbounds, searchTerm, statusFilter, now]);

  const totalUsers = inbounds?.reduce((acc, curr) => acc + curr.settings.clients.length, 0) || 0;

  const statusCounts = useMemo(() => {
    const allClients = inbounds?.flatMap((ib) => ib.settings.clients) ?? [];
    return allClients.reduce(
      (acc, c) => {
        const s = getClientStatus(c, now);
        acc[s] = (acc[s] || 0) + 1;
        return acc;
      },
      {} as Record<string, number>
    );
  }, [inbounds, now]);

  const routeOptions = useMemo(() => {
    const enabledOutboundTags = new Set(
      (outbounds || []).filter((o) => o.enable !== false).map((o) => o.tag)
    );
    return [
      { value: '', label: 'Default (No preference)' },
      ...(balancers
        ?.filter(
          (b) => b.enable !== false && b.selector.some((tag) => enabledOutboundTags.has(tag))
        )
        .map((b) => ({ value: b.tag, label: `Balancer: ${b.tag}` })) || []),
      ...(outbounds
        ?.filter((o) => o.enable !== false && !['direct', 'block'].includes(o.tag))
        .map((o) => ({ value: o.tag, label: `Server: ${o.tag}` })) || []),
    ];
  }, [outbounds, balancers]);

  return (
    <div className="space-y-6 pb-24 md:pb-10">
      {/* Stats row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 md:gap-4">
        <StatsCard
          title="Inbounds"
          value={inbounds?.length || 0}
          icon={<Network size={20} className="text-white" />}
          gradient="from-violet-600 to-indigo-600"
          delay={0}
        />
        <StatsCard
          title="Users"
          value={totalUsers}
          icon={<Users size={20} className="text-white" />}
          gradient="from-pink-600 to-rose-600"
          delay={0.05}
        />
        <StatsCard
          title="CPU"
          value={`${stats?.cpu || 0}%`}
          percentValue={stats?.cpu || 0}
          icon={<Cpu size={20} className="text-white" />}
          gradient="from-amber-500 to-orange-600"
          delay={0.1}
          isPercent
        />
        <StatsCard
          title="RAM"
          value={`${stats?.mem_percent || 0}%`}
          percentValue={stats?.mem_percent || 0}
          subtitle={`${stats?.mem_used || 0}G / ${stats?.mem_total || 0}G`}
          icon={<Activity size={20} className="text-white" />}
          gradient="from-emerald-500 to-teal-600"
          delay={0.15}
          isPercent
        />
      </div>

      {/* Header row */}
      <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 pt-1">
        <h2 className="text-xl font-bold text-white flex items-center gap-3">
          <span className="w-1 h-6 bg-gradient-to-b from-primary to-violet-600 rounded-full"></span>
          Active Connections
        </h2>
        <div className="relative w-full md:w-64">
          <Search
            className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none z-10"
            size={15}
          />
          <Input
            placeholder="Search user or inbound..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="pl-9 pr-9 bg-white/[0.04] border-white/[0.07] hover:border-white/15 h-10"
          />
          {searchTerm && (
            <button
              onClick={() => setSearchTerm('')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-500 hover:text-white z-10 transition-colors"
            >
              <X size={13} />
            </button>
          )}
        </div>
      </div>

      {/* Status filter bar */}
      <div className="flex items-center gap-2 overflow-x-auto pb-1 scrollbar-none">
        <div className="relative flex items-center gap-1.5 bg-white/[0.04] p-1 rounded-2xl border border-white/[0.05] flex-nowrap">
          {STATUS_FILTERS.map((f) => {
            const isActive = statusFilter === f.key;
            const count = f.key === 'all' ? totalUsers : statusCounts[f.key] || 0;
            return (
              <button
                key={f.key}
                onClick={() => setStatusFilter(f.key)}
                className="relative flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold whitespace-nowrap transition-colors duration-150 z-10"
              >
                {isActive && (
                  <motion.div
                    layoutId="statusFilterPill"
                    className={cn('absolute inset-0 rounded-xl border', f.pillCls)}
                    style={{ zIndex: -1 }}
                    transition={{ type: 'spring', stiffness: 500, damping: 35 }}
                  />
                )}
                {f.key !== 'all' && (
                  <span className="relative">
                    <span className={cn('block w-2 h-2 rounded-full', f.dotCls)} />
                    {f.key === 'online' && isActive && (
                      <span
                        className={cn(
                          'absolute inset-0 w-2 h-2 rounded-full animate-ping opacity-75',
                          f.dotCls
                        )}
                      />
                    )}
                  </span>
                )}
                <span
                  className={cn(
                    'transition-colors duration-150',
                    isActive ? f.textCls : 'text-gray-400'
                  )}
                >
                  {f.label}
                </span>
                {count > 0 && (
                  <span
                    className={cn(
                      'text-[10px] font-bold px-1.5 py-0.5 rounded-full min-w-[20px] text-center transition-colors duration-150',
                      isActive ? cn(f.textCls, 'bg-white/10') : 'text-gray-500 bg-white/[0.05]'
                    )}
                  >
                    {count}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>

      {/* Inbound list */}
      <div className="space-y-5">
        {isLoading && !inbounds && (
          <>
            <SkeletonCard />
            <SkeletonCard />
          </>
        )}

        {isError && !inbounds && (
          <div className="flex flex-col items-center justify-center py-16 border border-red-500/10 rounded-3xl bg-red-500/5">
            <p className="text-red-400 font-medium">Failed to load configurations</p>
            <p className="text-sm text-gray-500 mt-1">Retrying automatically...</p>
          </div>
        )}

        {!isLoading && inbounds && inbounds.length === 0 && (
          <motion.div
            initial={{ opacity: 0, scale: 0.97 }}
            animate={{ opacity: 1, scale: 1 }}
            className="flex flex-col items-center justify-center py-20 border border-white/[0.06] rounded-3xl bg-gradient-to-b from-white/[0.02] to-transparent"
          >
            <div className="p-4 rounded-2xl bg-white/[0.06] border border-white/[0.08] mb-5 shadow-inner">
              <Network size={32} className="text-gray-500" />
            </div>
            <h3 className="text-lg font-bold text-gray-300">No Inbounds Found</h3>
            <p className="text-sm text-gray-500 mt-1.5 max-w-xs text-center">
              Create your first inbound to start accepting connections.
            </p>
          </motion.div>
        )}

        {!isLoading && filteredInbounds.length === 0 && inbounds && inbounds.length > 0 && (
          <div className="text-center py-12 border border-white/5 rounded-3xl bg-white/[0.01]">
            {searchTerm ? (
              <p className="text-gray-400">
                No results for <span className="text-white font-medium">"{searchTerm}"</span>
              </p>
            ) : (
              <p className="text-gray-400">
                No users with status{' '}
                <span className="text-white font-medium">
                  {STATUS_FILTERS.find((f) => f.key === statusFilter)?.label}
                </span>
              </p>
            )}
          </div>
        )}

        {(() => {
          const isFirstLoad =
            !hasShownInboundsRef.current && filteredInbounds.length > 0 && !searchTerm;
          if (isFirstLoad) hasShownInboundsRef.current = true;
          return filteredInbounds.map((ib, i) => (
            <motion.div
              key={ib.tag}
              layout="position"
              initial={isFirstLoad ? { opacity: 0, y: 20 } : false}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                layout: { type: 'spring', stiffness: 350, damping: 35 },
                duration: 0.4,
                delay: isFirstLoad ? i * 0.08 : 0,
                ease: [0.25, 0.46, 0.45, 0.94],
              }}
            >
              <InboundCard
                inbound={ib}
                now={now}
                routeOptions={routeOptions}
                selectedUsers={selectedUsers}
                onToggleUser={toggleUser}
                onSelectAll={selectAllInInbound}
                onDeselectAll={deselectAllInInbound}
              />
            </motion.div>
          ));
        })()}
      </div>

      {/* Bulk Action Toolbar */}
      <BulkToolbar selectedUsers={selectedUsers} clearSelection={clearSelection} />

      {/* FAB */}
      <motion.div
        className="fixed bottom-6 right-6 md:bottom-8 md:right-8 z-30"
        initial={{ scale: 0, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ delay: 0.3, type: 'spring', stiffness: 400, damping: 25 }}
        whileHover={{ scale: 1.05 }}
        whileTap={{ scale: 0.95 }}
      >
        <Button
          onClick={() => setCreateModal(true)}
          className="h-14 w-14 md:w-auto md:px-7 rounded-full shadow-[0_0_32px_rgba(208,188,255,0.35)] bg-primary text-[#381E72] font-bold text-base border-2 border-white/20 flex items-center justify-center"
        >
          <Plus size={22} className="md:mr-2" />
          <span className="hidden md:inline">New Inbound</span>
        </Button>
      </motion.div>

      <Modal
        isOpen={createModal}
        onClose={() => setCreateModal(false)}
        title="New Inbound"
        maxWidth="max-w-2xl"
      >
        <InboundForm
          onSuccess={() => setCreateModal(false)}
          onCancel={() => setCreateModal(false)}
        />
      </Modal>
    </div>
  );
}

// ─── StatsCard ───────────────────────────────────────────────────────────────

interface StatsCardProps {
  icon: React.ReactNode;
  title: string;
  value: string | number;
  subtitle?: string;
  gradient: string;
  delay: number;
  isPercent?: boolean;
  percentValue?: number;
}

function StatsCard({
  icon,
  title,
  value,
  subtitle,
  gradient,
  delay,
  isPercent,
  percentValue,
}: StatsCardProps) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.35, delay }}
      whileHover={{ y: -3, transition: { duration: 0.2 } }}
      className="relative overflow-hidden rounded-2xl bg-[#1a1625]/70 backdrop-blur-xl border border-white/[0.06] p-4 md:p-5 group cursor-default"
    >
      {/* Background glow blob */}
      <div
        className={`absolute -right-6 -top-6 w-20 h-20 md:w-28 md:h-28 bg-gradient-to-br ${gradient} opacity-15 blur-2xl rounded-full group-hover:opacity-30 transition-opacity duration-500`}
      />
      {/* Hover border glow */}
      <div
        className={`absolute inset-0 rounded-2xl opacity-0 group-hover:opacity-100 transition-opacity duration-500 bg-gradient-to-br ${gradient} blur-md -z-10 scale-105`}
        style={{ opacity: 0 }}
      />

      <div className="flex justify-between items-start mb-3">
        <div>
          <p className="text-gray-500 text-[10px] md:text-xs font-semibold uppercase tracking-widest">
            {title}
          </p>
          <h3 className="text-2xl md:text-3xl font-bold text-white mt-1 tracking-tight tabular-nums">
            {typeof value === 'number' ? <AnimatedCounter value={value} /> : value}
          </h3>
        </div>
        <div
          className={`p-2.5 md:p-3 rounded-xl bg-gradient-to-br ${gradient} shadow-lg shadow-black/30`}
        >
          {icon}
        </div>
      </div>

      {isPercent && (
        <div className="w-full h-1 bg-white/[0.06] rounded-full overflow-hidden mt-1">
          <motion.div
            initial={{ width: '0%' }}
            animate={{ width: `${percentValue ?? 0}%` }}
            transition={{ duration: 0.8, delay: delay + 0.15, ease: 'easeOut' }}
            className={`h-full bg-gradient-to-r ${gradient} rounded-full`}
          />
        </div>
      )}
      {subtitle && (
        <p className="text-[10px] md:text-xs text-gray-500 mt-2 font-mono">{subtitle}</p>
      )}
    </motion.div>
  );
}

// ─── BulkToolbar ────────────────────────────────────────────────────────────

function BulkToolbar({
  selectedUsers,
  clearSelection,
}: {
  selectedUsers: Set<string>;
  clearSelection: () => void;
}) {
  const queryClient = useQueryClient();
  const [confirmBulkDelete, setConfirmBulkDelete] = useState(false);
  const [confirmBulkEnable, setConfirmBulkEnable] = useState(false);
  const [confirmBulkDisable, setConfirmBulkDisable] = useState(false);
  const [confirmBulkReset, setConfirmBulkReset] = useState(false);
  const [groupsModal, setGroupsModal] = useState(false);
  const [draftGroups, setDraftGroups] = useState<string[]>([]);

  const { data: nodes = [] } = useQuery<Node[]>({
    queryKey: ['nodes'],
    queryFn: () => api.get('/nodes').then((r) => r.data),
    staleTime: 60_000,
  });
  const { data: master } = useQuery<MasterInfo>({
    queryKey: ['nodes', 'master'],
    queryFn: () => api.get('/nodes/master').then((r) => r.data),
    staleTime: 60_000,
  });
  const tagSuggestions = Array.from(
    new Set([...nodes.flatMap((n) => n.groups || []), ...(master?.groups || [])])
  ).sort();

  const parseUsers = () =>
    [...selectedUsers].map((k) => {
      const idx = k.indexOf('\0');
      return { tag: k.slice(0, idx), email: k.slice(idx + 1) };
    });

  const onSuccess = (msg: string) => {
    queryClient.invalidateQueries({ queryKey: ['inbounds'] });
    clearSelection();
    toast.success(msg);
  };

  const bulkDeleteMutation = useMutation({
    mutationFn: () => api.post('/users/bulk-delete', { users: parseUsers() }),
    onSuccess: () => {
      onSuccess(`${selectedUsers.size} user(s) deleted`);
      setConfirmBulkDelete(false);
    },
    onError: (e: any) => toast.error(e.response?.data?.error || 'Bulk delete failed'),
  });

  const bulkResetMutation = useMutation({
    mutationFn: () => api.post('/users/reset-traffic', { users: parseUsers() }),
    onSuccess: () => {
      onSuccess(`Traffic reset for ${selectedUsers.size} user(s)`);
      setConfirmBulkReset(false);
    },
    onError: (e: any) => toast.error(e.response?.data?.error || 'Bulk reset failed'),
  });

  const bulkEnableMutation = useMutation({
    mutationFn: (enable: boolean) =>
      api.post('/users/bulk-enable', { users: parseUsers(), enable }),
    onSuccess: (_data, enable) => {
      onSuccess(`${selectedUsers.size} user(s) ${enable ? 'enabled' : 'disabled'}`);
      setConfirmBulkEnable(false);
      setConfirmBulkDisable(false);
    },
    onError: (e: any) => toast.error(e.response?.data?.error || 'Bulk enable failed'),
  });

  const bulkGroupsMutation = useMutation({
    mutationFn: (groups: string[]) =>
      api.post('/users/bulk-groups', { users: parseUsers(), allowed_node_groups: groups }),
    onSuccess: () => {
      onSuccess(`Groups updated for ${selectedUsers.size} user(s)`);
      setGroupsModal(false);
    },
    onError: (e: any) => toast.error(e.response?.data?.error || 'Bulk groups update failed'),
  });

  return (
    <>
      <AnimatePresence>
        {selectedUsers.size > 0 && (
          <motion.div
            initial={{ opacity: 0, y: 40 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 40 }}
            transition={{ type: 'spring', stiffness: 500, damping: 35 }}
            className="fixed bottom-6 left-0 right-0 md:pl-[96px] z-50 flex justify-center pointer-events-none"
          >
            <div className="pointer-events-auto flex items-center gap-2 px-4 py-2.5 bg-[#1a1722]/95 border border-white/10 backdrop-blur-xl rounded-2xl shadow-[0_8px_32px_rgba(0,0,0,0.5)]">
              <span className="text-sm font-semibold text-white tabular-nums mr-1">
                {selectedUsers.size} selected
              </span>
              <div className="w-px h-5 bg-white/10" />
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setConfirmBulkEnable(true)}
                className="text-emerald-400 hover:bg-emerald-500/10"
              >
                <Power size={13} className="mr-1" /> Enable
              </Button>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setConfirmBulkDisable(true)}
                className="text-orange-400 hover:bg-orange-500/10"
              >
                <PowerOff size={13} className="mr-1" /> Disable
              </Button>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setConfirmBulkReset(true)}
                className="text-yellow-400 hover:bg-yellow-500/10"
              >
                <RotateCcw size={13} className="mr-1" /> Reset
              </Button>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => {
                  setDraftGroups([]);
                  setGroupsModal(true);
                }}
                className="text-cyan-400 hover:bg-cyan-500/10"
              >
                <Tag size={13} className="mr-1" /> Groups
              </Button>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setConfirmBulkDelete(true)}
                className="text-red-400 hover:bg-red-500/10"
              >
                <Trash2 size={13} className="mr-1" /> Delete
              </Button>
              <div className="w-px h-5 bg-white/10" />
              <Button variant="ghost" size="sm" onClick={clearSelection}>
                <X size={13} />
              </Button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <ConfirmationModal
        isOpen={confirmBulkDelete}
        onClose={() => setConfirmBulkDelete(false)}
        onConfirm={() => bulkDeleteMutation.mutate()}
        title="Delete Selected Users"
        description={`Permanently delete ${selectedUsers.size} selected user(s)? This action cannot be undone.`}
        isLoading={bulkDeleteMutation.isPending}
      />

      <ConfirmationModal
        isOpen={confirmBulkEnable}
        onClose={() => setConfirmBulkEnable(false)}
        onConfirm={() => bulkEnableMutation.mutate(true)}
        title="Enable Selected Users"
        description={`Enable ${selectedUsers.size} selected user(s)? They will be able to connect again.`}
        confirmText="Enable"
        confirmVariant="primary"
        isLoading={bulkEnableMutation.isPending}
      />

      <ConfirmationModal
        isOpen={confirmBulkDisable}
        onClose={() => setConfirmBulkDisable(false)}
        onConfirm={() => bulkEnableMutation.mutate(false)}
        title="Disable Selected Users"
        description={`Disable ${selectedUsers.size} selected user(s)? Active connections will be dropped.`}
        confirmText="Disable"
        isLoading={bulkEnableMutation.isPending}
      />

      <ConfirmationModal
        isOpen={confirmBulkReset}
        onClose={() => setConfirmBulkReset(false)}
        onConfirm={() => bulkResetMutation.mutate()}
        title="Reset Selected Users"
        description={`Reset traffic counters for ${selectedUsers.size} selected user(s)? Their up/down counters will be set to zero.`}
        confirmText="Reset"
        isLoading={bulkResetMutation.isPending}
      />

      <Modal
        isOpen={groupsModal}
        onClose={() => setGroupsModal(false)}
        title="Set Node Groups"
        maxWidth="max-w-sm"
      >
        <div className="space-y-4 pt-2">
          <p className="text-sm text-gray-400">
            Assign node groups to {selectedUsers.size} selected user(s). Leave empty for all nodes.
          </p>
          <TagInput
            value={draftGroups}
            onChange={setDraftGroups}
            suggestions={tagSuggestions}
            placeholder="Type a tag and press Enter (blank = all nodes)"
            helperText="Users will only be provisioned on nodes carrying any of these tags"
          />
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" onClick={() => setGroupsModal(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => bulkGroupsMutation.mutate(draftGroups)}
              isLoading={bulkGroupsMutation.isPending}
            >
              Apply Groups
            </Button>
          </div>
        </div>
      </Modal>
    </>
  );
}

// ─── InboundCard ─────────────────────────────────────────────────────────────

function InboundCard({
  inbound,
  now,
  routeOptions,
  selectedUsers,
  onToggleUser,
  onSelectAll,
  onDeselectAll,
}: {
  inbound: Inbound;
  now: number;
  routeOptions: any[];
  selectedUsers: Set<string>;
  onToggleUser: (tag: string, email: string) => void;
  onSelectAll: (tag: string, emails: string[]) => void;
  onDeselectAll: (tag: string, emails: string[]) => void;
}) {
  const [editModal, setEditModal] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [userEmail, setUserEmail] = useState('');
  const supportsPanelUsers = !['socks', 'http'].includes(inbound.protocol);
  const queryClient = useQueryClient();

  const protocolBadge =
    PROTOCOL_COLORS[inbound.protocol] || 'bg-white/10 text-gray-300 border-white/10';
  const headerGlow = PROTOCOL_GLOW[inbound.protocol] || 'from-white/5';
  const hasTraffic = inbound.up + inbound.down > 0;

  const deleteMutation = useMutation({
    mutationFn: () => api.delete(`/inbounds/${inbound.tag}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['inbounds'] });
      toast.success(`Inbound ${inbound.tag} deleted`);
      setConfirmDelete(false);
    },
  });

  const resetInboundMutation = useMutation({
    mutationFn: () => api.post(`/inbounds/${inbound.tag}/reset-traffic`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['inbounds'] });
      toast.success('Traffic stats reset successfully');
      setConfirmReset(false);
    },
  });

  const addUserMutation = useMutation({
    mutationFn: () => api.post(`/inbounds/${inbound.tag}/users`, { email: userEmail }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['inbounds'] });
      setUserEmail('');
      toast.success('User added successfully');
    },
    onError: (e: any) => toast.error(e.response?.data?.error || 'Failed to add user'),
  });

  return (
    <div className="group bg-[#1a1722]/90 border border-white/[0.06] rounded-3xl overflow-hidden hover:border-white/[0.12] transition-colors duration-300 shadow-xl">
      {/* Card header */}
      <div
        className={`p-5 md:p-6 border-b border-white/[0.05] flex flex-col lg:flex-row justify-between items-start lg:items-center gap-4 bg-gradient-to-r ${headerGlow} to-transparent`}
      >
        <div className="flex items-center gap-4 w-full lg:w-auto">
          {supportsPanelUsers &&
            inbound.settings.clients.length > 0 &&
            (() => {
              const emails = inbound.settings.clients.map((c) => c.email);
              const keys = emails.map((e) => `${inbound.tag}\0${e}`);
              const allSel = keys.every((k) => selectedUsers.has(k));
              const someSel = keys.some((k) => selectedUsers.has(k));
              const Icon = allSel ? CheckSquare : someSel ? Minus : Square;
              return (
                <button
                  onClick={() =>
                    allSel ? onDeselectAll(inbound.tag, emails) : onSelectAll(inbound.tag, emails)
                  }
                  className="shrink-0 text-gray-500 hover:text-primary transition-colors"
                  title={allSel ? 'Deselect all' : 'Select all'}
                >
                  <Icon size={18} />
                </button>
              );
            })()}
          <div className="h-12 w-12 shrink-0 rounded-2xl bg-gradient-to-br from-white/[0.07] to-white/[0.02] border border-white/[0.08] flex items-center justify-center shadow-inner">
            <Zap
              size={22}
              className={hasTraffic ? 'text-primary fill-primary/30' : 'text-gray-600'}
            />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2.5 flex-wrap">
              <h3 className="text-base md:text-lg font-bold text-white tracking-tight truncate">
                {inbound.tag}
              </h3>
              <span
                className={cn(
                  'px-2 py-0.5 text-[10px] font-bold rounded-md uppercase tracking-wide border',
                  protocolBadge
                )}
              >
                {inbound.protocol}
              </span>
            </div>
            <div className="flex items-center gap-3 mt-1.5 flex-wrap">
              <div className="text-[10px] md:text-xs text-gray-400 font-mono bg-black/25 px-2 py-0.5 rounded border border-white/[0.06]">
                :{inbound.port}
              </div>
              <div className="flex gap-2.5 text-[10px] md:text-[11px] font-mono text-gray-400">
                <span className="flex items-center gap-1">
                  <ArrowUp size={10} className="text-green-400" /> {formatBytes(inbound.up)}
                </span>
                <span className="flex items-center gap-1">
                  <ArrowDown size={10} className="text-blue-400" /> {formatBytes(inbound.down)}
                </span>
              </div>
            </div>
          </div>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 lg:flex gap-2 w-full lg:w-auto">
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setConfirmReset(true)}
            className="w-full lg:w-auto justify-center"
          >
            <RotateCcw size={13} className="mr-1.5" /> Reset
          </Button>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setEditModal(true)}
            className="w-full lg:w-auto justify-center"
          >
            <Edit size={13} className="mr-1.5" /> Config
          </Button>
          <Button
            variant="danger"
            size="sm"
            onClick={() => setConfirmDelete(true)}
            className="col-span-2 md:col-span-1 w-full lg:w-auto justify-center"
          >
            <Trash2 size={13} />
          </Button>
        </div>
      </div>

      {/* Card body — AnimatedHeight measures content and springs to new height */}
      <AnimatedHeight>
        <div className="p-4 md:p-5">
          {supportsPanelUsers ? (
            <>
              <div className="space-y-2.5">
                <AnimatePresence>
                  {inbound.settings.clients.map((c) => (
                    <UserRow
                      key={c.id}
                      client={c}
                      inbound={inbound}
                      now={now}
                      routeOptions={routeOptions}
                      isSelected={selectedUsers.has(`${inbound.tag}\0${c.email}`)}
                      onToggleSelect={() => onToggleUser(inbound.tag, c.email)}
                    />
                  ))}
                </AnimatePresence>

                {inbound.settings.clients.length === 0 && (
                  <div className="text-center py-8 border-2 border-dashed border-white/[0.07] rounded-2xl bg-white/[0.01]">
                    <p className="text-gray-500 text-sm">No users yet</p>
                  </div>
                )}
              </div>

              <div className="mt-5 pt-5 border-t border-white/[0.05] flex flex-col md:flex-row gap-2.5">
                <Input
                  placeholder="New user email / username"
                  value={userEmail}
                  onChange={(e) => setUserEmail(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && userEmail && addUserMutation.mutate()}
                  className="bg-black/20 border-white/[0.07] hover:border-white/15 h-10"
                />
                <Button
                  className="h-10 px-5 bg-white/[0.07] hover:bg-white/[0.12] text-white border border-white/[0.09] w-full md:w-auto shrink-0"
                  onClick={() => userEmail && addUserMutation.mutate()}
                  isLoading={addUserMutation.isPending}
                >
                  <Plus size={16} className="mr-1.5" /> Add User
                </Button>
              </div>
            </>
          ) : (
            <div className="py-8 border-2 border-dashed border-white/[0.07] rounded-2xl text-center bg-white/[0.01]">
              <p className="text-gray-400 text-sm">Panel users disabled for this protocol.</p>
              <p className="text-gray-500 text-xs mt-1">Set credentials in inbound config.</p>
            </div>
          )}
        </div>
      </AnimatedHeight>

      <Modal
        isOpen={editModal}
        onClose={() => setEditModal(false)}
        title={`Edit ${inbound.tag}`}
        maxWidth="max-w-2xl"
      >
        <InboundForm
          inbound={inbound}
          onSuccess={() => setEditModal(false)}
          onCancel={() => setEditModal(false)}
        />
      </Modal>

      <ConfirmationModal
        isOpen={confirmDelete}
        onClose={() => setConfirmDelete(false)}
        onConfirm={() => deleteMutation.mutate()}
        title="Delete Inbound"
        description={`Are you sure you want to delete inbound "${inbound.tag}"? This action cannot be undone.`}
        isLoading={deleteMutation.isPending}
      />
      <ConfirmationModal
        isOpen={confirmReset}
        onClose={() => setConfirmReset(false)}
        onConfirm={() => resetInboundMutation.mutate()}
        title="Reset Inbound Traffic"
        description={`This will reset all traffic stats to zero for inbound "${inbound.tag}". This cannot be undone.`}
        isLoading={resetInboundMutation.isPending}
      />
    </div>
  );
}

// ─── UserRow ──────────────────────────────────────────────────────────────────

function trafficBarColor(percent: number) {
  if (percent >= 90) return 'bg-red-500';
  if (percent >= 70) return 'bg-amber-400';
  return 'bg-primary';
}

function UserRow({
  client,
  inbound,
  now,
  routeOptions,
  isSelected,
  onToggleSelect,
}: {
  client: Client;
  inbound: Inbound;
  now: number;
  routeOptions: any[];
  isSelected: boolean;
  onToggleSelect: () => void;
}) {
  const [qr, setQr] = useState(false);
  const [edit, setEdit] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [routingModal, setRoutingModal] = useState(false);
  const [selectedRoute, setSelectedRoute] = useState(client.preferred_outbound || '');
  const [devicesExpanded, setDevicesExpanded] = useState(false);
  const [revokeTarget, setRevokeTarget] = useState<ClientDevice | null>(null);
  const [revokeLoading, setRevokeLoading] = useState(false);

  const effectiveDeviceLimit = client.device_limit ?? inbound.device_limit ?? 0;

  useEffect(() => {
    setSelectedRoute(client.preferred_outbound || '');
  }, [client.preferred_outbound]);

  const queryClient = useQueryClient();
  const link = generateLink(inbound, client);
  const subscriptionUrl = generateSubscriptionUrl(client);

  const resetMutation = useMutation({
    mutationFn: () => api.post('/users/reset-traffic', { tag: inbound.tag, email: client.email }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['inbounds'] });
      toast.success('Traffic reset');
      setConfirmReset(false);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () =>
      api.delete(`/inbounds/${inbound.tag}/users`, { params: { email: client.email } }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['inbounds'] });
      toast.success('User deleted');
      setConfirmDel(false);
    },
  });

  const routingMutation = useMutation({
    mutationFn: (outbound_tag: string) =>
      api.post('/user/routing', { email: client.email, inbound_tag: inbound.tag, outbound_tag }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['inbounds'] });
      toast.success('Routing updated');
      setRoutingModal(false);
    },
    onError: (e: any) => {
      toast.error(e.response?.data?.error || 'Failed to update routing');
    },
  });

  // Devices query — auto-refresh every 3s while expanded so the list stays in
  // sync with the chip count (which is itself driven by the inbounds poll).
  const devicesQueryKey = ['client-devices', client.id];
  const devicesQuery = useQuery<ClientDevice[]>({
    queryKey: devicesQueryKey,
    queryFn: async () => {
      const res = await api.get<ClientDevice[]>(`/clients/${client.id}/devices`);
      return res.data;
    },
    enabled: devicesExpanded,
    refetchInterval: devicesExpanded ? 3000 : false,
    refetchOnWindowFocus: false,
  });
  const devices = devicesQuery.data ?? null;
  const devicesLoading = devicesQuery.isFetching && !devicesQuery.data;
  const devicesError = devicesQuery.error
    ? (devicesQuery.error as any).response?.data?.error || 'Failed to load devices'
    : null;

  const toggleDevices = async () => {
    if (devicesLoading) return;
    if (devicesExpanded) {
      setDevicesExpanded(false);
      return;
    }
    // Pre-fetch BEFORE expanding so the spring animates straight to the final
    // height instead of stuttering when the loading placeholder is replaced.
    if (!devicesQuery.data) {
      await queryClient.fetchQuery({
        queryKey: devicesQueryKey,
        queryFn: async () => {
          const res = await api.get<ClientDevice[]>(`/clients/${client.id}/devices`);
          return res.data;
        },
      });
    }
    setDevicesExpanded(true);
  };

  const onRevokeDevice = (d: ClientDevice) => {
    setRevokeTarget(d);
  };

  const confirmRevokeDevice = async () => {
    if (!revokeTarget) return;
    setRevokeLoading(true);
    try {
      await api.delete(`/clients/${client.id}/devices/${revokeTarget.id}`);
      // Optimistic local removal so the row disappears instantly; query will
      // refetch on next tick and confirm.
      queryClient.setQueryData<ClientDevice[]>(devicesQueryKey, (prev) =>
        prev ? prev.filter((x) => x.id !== revokeTarget.id) : prev
      );
      queryClient.invalidateQueries({ queryKey: ['inbounds'] });
      queryClient.invalidateQueries({ queryKey: devicesQueryKey });
      toast.success('Device revoked');
      setRevokeTarget(null);
    } catch (e: any) {
      toast.error(e.response?.data?.error || 'Failed to revoke device');
    } finally {
      setRevokeLoading(false);
    }
  };

  const usagePercent = client.limit_bytes
    ? Math.min(100, ((client.up + client.down) / client.limit_bytes) * 100)
    : 0;
  const status = getClientStatus(client, now);
  const barColor = trafficBarColor(usagePercent);

  // Avatar gradient based on first letter
  const avatarHue = ((client.email.charCodeAt(0) || 65) * 137) % 360;

  return (
    <motion.div
      layout="position"
      variants={{
        initial: { opacity: 0, y: 6 },
        animate: {
          opacity: 1,
          y: 0,
          transition: { duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] },
        },
        exit: { opacity: 0, x: -20, transition: { duration: 0.18, ease: [0.4, 0, 1, 1] } },
      }}
      transition={{ layout: { type: 'spring', stiffness: 400, damping: 35 } }}
      initial="initial"
      animate="animate"
      exit="exit"
      className={cn(
        'group/row relative flex flex-col p-3.5 rounded-xl border transition-colors duration-100 overflow-hidden',
        status === 'disabled'
          ? 'bg-rose-500/[0.04] border-rose-500/[0.08] opacity-70'
          : status === 'expired'
            ? 'bg-amber-500/[0.04] border-amber-500/[0.08] opacity-75'
            : status === 'overlimit'
              ? 'bg-red-500/[0.05] border-red-500/[0.10] opacity-80'
              : 'bg-white/[0.025] border-white/[0.05] hover:bg-white/[0.04] hover:border-primary/20'
      )}
    >
      {/* Traffic usage bar at bottom */}
      {client.limit_bytes > 0 && (
        <div className="absolute bottom-0 left-0 h-[2px] w-full bg-white/[0.04]">
          <motion.div
            initial={{ width: '0%' }}
            animate={{ width: `${usagePercent}%` }}
            transition={{ duration: 0.8, ease: 'easeOut' }}
            className={cn('h-full rounded-full transition-colors duration-500', barColor)}
          />
        </div>
      )}

      <div className="flex flex-col xl:flex-row xl:items-center justify-between">
        {/* Left: avatar + info */}
        <div className="flex items-center gap-3 mb-3 xl:mb-0 min-w-0">
          <button
            onClick={(e) => {
              e.stopPropagation();
              onToggleSelect();
            }}
            className={cn(
              'shrink-0 overflow-hidden flex items-center justify-center transition-[width,opacity,margin] duration-200 ease-out',
              isSelected
                ? 'w-4 opacity-100 text-primary'
                : 'w-0 -mr-3 opacity-0 text-gray-500 group-hover/row:w-4 group-hover/row:mr-0 group-hover/row:opacity-100'
            )}
          >
            {isSelected ? <CheckSquare size={16} /> : <Square size={16} />}
          </button>
          <div className="relative shrink-0">
            <div
              className="w-9 h-9 rounded-full flex items-center justify-center text-sm font-bold shadow-inner"
              style={{
                background: client.enable
                  ? `linear-gradient(135deg, hsl(${avatarHue},40%,30%), hsl(${avatarHue},30%,20%))`
                  : 'rgba(60,60,60,0.8)',
                color: client.enable ? `hsl(${avatarHue},60%,80%)` : '#666',
              }}
            >
              {client.email[0].toUpperCase()}
            </div>
            <div className="absolute -bottom-0.5 -right-0.5 w-3 h-3">
              {status === 'online' && (
                <span className="absolute inset-0 rounded-full bg-emerald-400 animate-ping opacity-70" />
              )}
              <span
                className={cn(
                  'absolute inset-0 rounded-full border-2 border-[#1a1722] transition-colors duration-300',
                  status === 'online'
                    ? 'bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.8)]'
                    : status === 'offline'
                      ? 'bg-zinc-600'
                      : status === 'expired'
                        ? 'bg-amber-400'
                        : status === 'overlimit'
                          ? 'bg-red-400'
                          : 'bg-rose-600'
                )}
              />
            </div>
          </div>

          <div className="min-w-0 overflow-hidden">
            <div
              className="text-sm font-semibold text-gray-200 truncate flex items-center gap-2"
              title={client.email}
            >
              {client.email}
              {client.preferred_outbound && (
                <span className="px-1.5 py-0.5 rounded text-[10px] bg-primary/15 text-primary uppercase border border-primary/20 font-bold tracking-wide">
                  {client.preferred_outbound}
                </span>
              )}
            </div>
            <div className="text-[10px] text-gray-500 font-mono mt-0.5">
              {client.expiry_time
                ? `Exp: ${new Date(client.expiry_time).toLocaleDateString()}`
                : 'No expiry'}
            </div>
          </div>
        </div>

        {/* Right: traffic + actions */}
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 w-full xl:w-auto">
          <div className="flex items-center gap-2 flex-wrap">
            <div className="flex items-center gap-2 text-[10px] font-mono px-2.5 py-1.5 rounded-lg bg-black/20 border border-white/[0.05] w-fit">
              <span className="flex items-center gap-1 text-green-400">
                <ArrowUp size={9} /> {formatBytes(client.up)}
              </span>
              <span className="w-px h-3 bg-white/[0.1]" />
              <span className="flex items-center gap-1 text-blue-400">
                <ArrowDown size={9} /> {formatBytes(client.down)}
              </span>
              {client.limit_bytes > 0 && (
                <span
                  className={cn(
                    'ml-0.5 font-semibold',
                    usagePercent >= 90
                      ? 'text-red-400'
                      : usagePercent >= 70
                        ? 'text-amber-400'
                        : 'text-gray-500'
                  )}
                >
                  / {formatBytes(client.limit_bytes)}
                </span>
              )}
            </div>
            {(effectiveDeviceLimit > 0 || (client.device_count ?? 0) > 0) && (
              <button
                type="button"
                onClick={toggleDevices}
                title={devicesExpanded ? 'Hide devices' : 'Show devices'}
                className={cn(
                  'flex items-center gap-1.5 text-xs px-2 py-1 rounded-lg border transition-colors',
                  devicesExpanded
                    ? 'bg-primary/15 text-primary border-primary/25'
                    : 'bg-white/[0.06] text-white/70 border-white/[0.05] hover:bg-white/[0.09] hover:text-white'
                )}
              >
                <Smartphone size={11} />
                <span className="font-mono">
                  {client.device_count ?? 0}
                  {effectiveDeviceLimit > 0 ? ` / ${effectiveDeviceLimit}` : ''}
                </span>
                {devicesLoading ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <ChevronDown
                    size={12}
                    className={cn(
                      'transition-transform duration-200',
                      devicesExpanded && 'rotate-180'
                    )}
                  />
                )}
              </button>
            )}
          </div>

          <div className="grid grid-cols-7 gap-1 sm:flex">
            <Button
              variant="secondary"
              size="icon"
              className="h-8 w-full sm:w-8 text-gray-400 hover:text-white"
              onClick={() => setQr(true)}
              title="QR Code"
            >
              <QrCode size={13} />
            </Button>
            <Button
              variant="secondary"
              size="icon"
              className="h-8 w-full sm:w-8 text-gray-400 hover:text-white"
              onClick={() => {
                navigator.clipboard.writeText(link);
                toast.success('Link copied');
              }}
              title="Copy link"
            >
              <Copy size={13} />
            </Button>
            <Button
              variant="secondary"
              size="icon"
              className="h-8 w-full sm:w-8 text-cyan-400 hover:text-cyan-200 hover:bg-cyan-500/10"
              onClick={() => {
                navigator.clipboard.writeText(subscriptionUrl);
                toast.success('Subscription URL copied');
              }}
              title="Copy sub URL"
            >
              <Link2 size={13} />
            </Button>
            <Button
              variant="secondary"
              size="icon"
              className="h-8 w-full sm:w-8 text-indigo-400 hover:text-indigo-200 hover:bg-indigo-500/10"
              onClick={() => setRoutingModal(true)}
              title="Route"
            >
              <Network size={13} />
            </Button>
            <Button
              variant="secondary"
              size="icon"
              className="h-8 w-full sm:w-8 text-gray-400 hover:text-white"
              onClick={() => setEdit(true)}
              title="Edit"
            >
              <Edit size={13} />
            </Button>
            <Button
              variant="secondary"
              size="icon"
              className="h-8 w-full sm:w-8 text-yellow-500/60 hover:text-yellow-400 hover:bg-yellow-500/10"
              onClick={() => setConfirmReset(true)}
              title="Reset traffic"
            >
              <RotateCcw size={13} />
            </Button>
            <Button
              variant="secondary"
              size="icon"
              className="h-8 w-full sm:w-8 text-red-500/60 hover:text-red-400 hover:bg-red-500/10"
              onClick={() => setConfirmDel(true)}
              title="Delete"
            >
              <Trash2 size={13} />
            </Button>
          </div>
        </div>
      </div>

      <AnimatePresence initial={false}>
        {devicesExpanded && (effectiveDeviceLimit > 0 || (client.device_count ?? 0) > 0) && (
          <motion.div
            key="devices"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ type: 'spring', stiffness: 350, damping: 35 }}
            style={{ overflow: 'hidden' }}
          >
            <div className="mt-3 pt-3 border-t border-white/[0.05] space-y-1.5">
              {devicesLoading && (
                <div className="text-xs text-gray-500 italic px-1">Loading devices…</div>
              )}
              {devicesError && <div className="text-xs text-red-400 px-1">{devicesError}</div>}
              {!devicesLoading && !devicesError && devices && devices.length === 0 && (
                <div className="text-xs text-gray-500 italic px-1">No devices registered yet.</div>
              )}
              {!devicesLoading &&
                devices &&
                devices.map((d) => (
                  <div
                    key={d.id}
                    className="flex items-start gap-3 p-2.5 rounded-lg bg-black/20 border border-white/[0.05]"
                  >
                    <span className="text-base leading-none mt-0.5" aria-hidden>
                      {deviceIcon(d.device_os)}
                    </span>
                    <div className="min-w-0 flex-1">
                      <div className="text-xs font-semibold text-gray-200 truncate">
                        {d.device_os || 'unknown'}
                        {d.model && <span className="text-gray-400 font-normal"> · {d.model}</span>}
                      </div>
                      <div className="text-[10px] text-gray-500 font-mono mt-0.5 truncate">
                        {d.os_ver && <span>{d.os_ver}</span>}
                        {d.user_agent && <span> · UA: {d.user_agent}</span>}
                        {d.request_ip && <span> · IP: {d.request_ip}</span>}
                      </div>
                      <div className="text-[10px] text-gray-500 mt-0.5">
                        first {timeAgo(d.first_seen)} · last {timeAgo(d.last_seen)}
                        {typeof d.hits === 'number' && <span> · {d.hits} hits</span>}
                      </div>
                    </div>
                    <Button
                      type="button"
                      variant="danger"
                      size="sm"
                      onClick={() => onRevokeDevice(d)}
                      className="shrink-0 self-center"
                    >
                      Revoke
                    </Button>
                  </div>
                ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <Modal isOpen={qr} onClose={() => setQr(false)} title="Connection QR">
        <div className="flex flex-col items-center">
          <div className="p-4 bg-white rounded-2xl shadow-xl mb-5">
            <QRCodeCanvas value={link} size={240} />
          </div>
          <div className="w-full bg-black/40 p-4 rounded-xl border border-white/[0.07] break-all text-xs font-mono text-gray-400 text-center select-all">
            {link}
          </div>
        </div>
      </Modal>

      <Modal isOpen={edit} onClose={() => setEdit(false)} title="Edit User">
        <UserForm inbound={inbound} client={client} onClose={() => setEdit(false)} />
      </Modal>

      <Modal
        isOpen={routingModal}
        onClose={() => setRoutingModal(false)}
        title="Preferred Route"
        maxWidth="max-w-sm"
      >
        <div className="space-y-4 pt-2">
          <p className="text-sm text-gray-400">
            Select a specific server or balancer for this user. This overrides global routing rules.
          </p>
          <Select
            options={routeOptions}
            value={selectedRoute}
            onChange={(e) => setSelectedRoute(e.target.value)}
          />
          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" onClick={() => setRoutingModal(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => routingMutation.mutate(selectedRoute)}
              isLoading={routingMutation.isPending}
            >
              Save Preference
            </Button>
          </div>
        </div>
      </Modal>

      <ConfirmationModal
        isOpen={confirmDel}
        onClose={() => setConfirmDel(false)}
        onConfirm={() => deleteMutation.mutate()}
        title="Delete User"
        description={`Permanently delete user "${client.email}"? Their connection and all usage data will be removed.`}
        isLoading={deleteMutation.isPending}
      />
      <ConfirmationModal
        isOpen={confirmReset}
        onClose={() => setConfirmReset(false)}
        onConfirm={() => resetMutation.mutate()}
        title="Reset Traffic"
        description={`This will reset all uploaded and downloaded traffic counters for "${client.email}" back to zero. The user's connection and settings will not be affected.`}
        isLoading={resetMutation.isPending}
      />
      <ConfirmationModal
        isOpen={revokeTarget !== null}
        onClose={() => setRevokeTarget(null)}
        onConfirm={confirmRevokeDevice}
        title="Revoke device"
        description={
          revokeTarget
            ? `Revoke ${revokeTarget.device_os || 'device'}${revokeTarget.model ? ` (${revokeTarget.model})` : ''}? It will need to re-register on the next subscription fetch.`
            : ''
        }
        confirmText="Revoke"
        isLoading={revokeLoading}
      />
    </motion.div>
  );
}
