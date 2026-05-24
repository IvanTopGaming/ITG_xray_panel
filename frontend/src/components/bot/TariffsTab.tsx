import { useMemo, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { toast } from 'react-toastify';
import { Plus, Search } from 'lucide-react';
import { motion } from 'framer-motion';
import api from '@/lib/api';
import {
  listTariffs,
  archiveTariff,
  duplicateTariff,
  createTariff,
  updateTariff,
  deleteTariffPermanent,
  restoreTariff,
  getTariffStats,
} from '@/lib/bot';
import type { Inbound, Tariff, TariffStatsMap, TariffWritePayload } from '@/lib/types';
import { cn } from '@/lib/utils';
import { ConfirmationModal } from '@/components/ui/ConfirmationModal';
import { TrialCard } from './TrialCard';
import { TariffsTable } from './TariffsTable';
import { TariffDrawer } from './TariffDrawer';

type FilterValue = 'all' | 'public' | 'private' | 'archived';

const FILTERS: { value: FilterValue; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'public', label: 'Public' },
  { value: 'private', label: 'Private' },
  { value: 'archived', label: 'Archived' },
];

export function TariffsTab() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<FilterValue>('all');
  const [search, setSearch] = useState('');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editingTariff, setEditingTariff] = useState<Tariff | null>(null);
  const [drawerSeed, setDrawerSeed] = useState<{ is_trial: boolean }>({ is_trial: false });
  const [pendingDelete, setPendingDelete] = useState<Tariff | null>(null);
  const [pendingArchive, setPendingArchive] = useState<Tariff | null>(null);

  const tariffsQuery = useQuery({
    queryKey: ['bot', 'tariffs'],
    queryFn: listTariffs,
  });

  const statsQuery = useQuery({
    queryKey: ['bot', 'tariffs', 'stats'],
    queryFn: getTariffStats,
    staleTime: 30_000,
    refetchInterval: 30_000,
  });

  const inboundsQuery = useQuery({
    queryKey: ['inbounds'],
    queryFn: async () => (await api.get<Inbound[]>('/inbounds')).data,
    staleTime: 60_000,
  });

  const nodesQuery = useQuery({
    queryKey: ['nodes'],
    queryFn: async () => (await api.get<{ id: number; groups: string[] }[]>('/nodes')).data,
    staleTime: 60_000,
  });

  const tariffs = tariffsQuery.data ?? [];
  const stats: TariffStatsMap = statsQuery.data ?? {};
  const inbounds = inboundsQuery.data ?? [];
  const nodeGroups = useMemo(() => {
    const set = new Set<string>();
    for (const n of nodesQuery.data ?? []) {
      for (const g of n.groups) set.add(g);
    }
    return [...set].sort();
  }, [nodesQuery.data]);

  const trial = useMemo(() => tariffs.find((t) => t.is_trial) ?? null, [tariffs]);

  const paidTariffs = useMemo(() => tariffs.filter((t) => !t.is_trial), [tariffs]);

  const filteredPaid = useMemo(() => {
    let list = paidTariffs;
    if (filter !== 'all') {
      list = list.filter((t) => t.visibility === filter);
    }
    if (search.trim()) {
      const needle = search.trim().toLowerCase();
      list = list.filter(
        (t) =>
          t.name.toLowerCase().includes(needle) ||
          t.items.some((i) => i.inbound_tag.toLowerCase().includes(needle))
      );
    }
    return list;
  }, [paidTariffs, filter, search]);

  const counts = useMemo(() => {
    const c = { all: paidTariffs.length, public: 0, private: 0, archived: 0 };
    for (const t of paidTariffs) c[t.visibility]++;
    return c;
  }, [paidTariffs]);

  const summary = useMemo(() => {
    const totalActive = Object.values(stats).reduce((a, s) => a + s.active_subs, 0);
    const total30d = Object.values(stats).reduce((a, s) => a + s.revenue_30d, 0);
    return { totalActive, total30d };
  }, [stats]);

  const openCreate = (isTrial = false) => {
    setEditingTariff(null);
    setDrawerSeed({ is_trial: isTrial });
    setDrawerOpen(true);
  };

  const openEdit = (t: Tariff) => {
    setEditingTariff(t);
    setDrawerSeed({ is_trial: t.is_trial });
    setDrawerOpen(true);
  };

  const closeDrawer = () => setDrawerOpen(false);

  const saveMutation = useMutation({
    mutationFn: async (payload: TariffWritePayload) => {
      if (editingTariff) {
        await updateTariff(editingTariff.id, payload);
      } else {
        await createTariff(payload);
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bot', 'tariffs'] });
      toast.success(editingTariff ? 'Tariff updated' : 'Tariff created');
    },
  });

  const duplicateMutation = useMutation({
    mutationFn: duplicateTariff,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bot', 'tariffs'] });
      toast.success('Tariff duplicated');
    },
    onError: () => toast.error('Failed to duplicate'),
  });

  const archiveMutation = useMutation({
    mutationFn: archiveTariff,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bot', 'tariffs'] });
      toast.success('Tariff archived');
    },
    onError: () => toast.error('Failed to archive'),
  });

  const restoreMutation = useMutation({
    mutationFn: restoreTariff,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bot', 'tariffs'] });
      toast.success('Tariff restored');
    },
    onError: () => toast.error('Failed to restore'),
  });

  const deleteMutation = useMutation({
    mutationFn: deleteTariffPermanent,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bot', 'tariffs'] });
      toast.success('Tariff deleted');
    },
    onError: (err: unknown) => {
      const e = err as { response?: { status?: number; data?: { payment_count?: number } } };
      if (e?.response?.status === 409) {
        const n = e.response.data?.payment_count ?? 0;
        toast.error(`Refused: ${n} payment(s) reference this tariff. Archive instead.`);
      } else {
        toast.error('Failed to delete');
      }
    },
  });

  const handleDelete = (t: Tariff) => setPendingDelete(t);

  if (tariffsQuery.isLoading) {
    return <div className="text-sm text-white/50">Loading tariffs…</div>;
  }
  if (tariffsQuery.error) {
    return <div className="text-sm text-rose-300">Failed to load tariffs.</div>;
  }

  const drawerSeedTariff = editingTariff;

  return (
    <div className="flex flex-col gap-5">
      {/* ── Page header ──────────────────────────────────────────── */}
      <div className="flex items-baseline justify-between">
        <div>
          <h2 className="text-xl font-bold text-white">Tariffs</h2>
          <p className="mt-0.5 text-xs text-white/45">
            {tariffs.length} tariff{tariffs.length === 1 ? '' : 's'} ·{' '}
            <span className="text-violet-300">{summary.totalActive}</span> active subscriber
            {summary.totalActive === 1 ? '' : 's'} ·{' '}
            <span className="text-emerald-300">{formatRub(summary.total30d)}</span> in last 30 days
          </p>
        </div>
      </div>

      {/* ── Trial card ──────────────────────────────────────────── */}
      <TrialCard
        trial={trial}
        stats={trial ? (stats[trial.id] ?? null) : null}
        onEdit={openEdit}
        onCreate={() => openCreate(true)}
      />

      {/* ── Toolbar ──────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-1 rounded-2xl border border-white/[0.05] bg-white/[0.04] p-1">
            {FILTERS.map((f) => {
              const count = counts[f.value as keyof typeof counts];
              const active = filter === f.value;
              return (
                <button
                  key={f.value}
                  onClick={() => setFilter(f.value)}
                  className={cn(
                    'relative rounded-xl px-3 py-1.5 text-[11px] font-bold uppercase tracking-wider transition-colors',
                    active ? 'text-white' : 'text-gray-400 hover:text-white'
                  )}
                >
                  {active && (
                    <motion.div
                      layoutId="tariffFilterPill"
                      className="absolute inset-0 rounded-xl border border-white/[0.10] bg-gradient-to-br from-primary/25 to-violet-600/20 shadow-[0_0_12px_rgba(208,188,255,0.12)]"
                      transition={{ type: 'spring', stiffness: 500, damping: 35 }}
                    />
                  )}
                  <span className="relative z-10">
                    {f.label}
                    <span className="ml-1.5 opacity-60">{count}</span>
                  </span>
                </button>
              );
            })}
          </div>
          <div className="relative">
            <Search
              size={13}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-white/30"
            />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search tariffs…"
              className="w-56 rounded-xl border border-white/[0.05] bg-white/[0.04] py-2 pl-9 pr-3 text-xs text-white placeholder:text-white/30 focus:border-violet-500/40 focus:outline-none"
            />
          </div>
        </div>
        <button
          type="button"
          onClick={() => openCreate(false)}
          className="flex items-center gap-1.5 rounded-xl border border-violet-500/30 bg-gradient-to-br from-violet-500/30 to-violet-600/20 px-4 py-2 text-xs font-semibold text-white shadow-[0_0_12px_rgba(208,188,255,0.12)] transition-colors hover:from-violet-500/40"
        >
          <Plus size={13} />
          New tariff
        </button>
      </div>

      {/* ── Table ──────────────────────────────────────────── */}
      <TariffsTable
        tariffs={filteredPaid}
        stats={stats}
        selectedId={drawerOpen ? (editingTariff?.id ?? null) : null}
        onRowClick={openEdit}
        onDuplicate={(t) => duplicateMutation.mutate(t.id)}
        onArchive={(t) => setPendingArchive(t)}
        onRestore={(t) => restoreMutation.mutate(t.id)}
        onDelete={handleDelete}
      />

      {/* ── Drawer ──────────────────────────────────────────── */}
      <TariffDrawer
        open={drawerOpen}
        tariff={drawerSeedTariff}
        stats={drawerSeedTariff ? (stats[drawerSeedTariff.id] ?? null) : null}
        inbounds={inbounds}
        nodeGroups={nodeGroups}
        saving={saveMutation.isPending}
        onClose={closeDrawer}
        onSave={async (payload) => {
          // If creating, propagate the seed (e.g. trial=true from TrialCard create)
          await saveMutation.mutateAsync({
            ...payload,
            is_trial: drawerSeed.is_trial || payload.is_trial,
          });
        }}
      />

      <ConfirmationModal
        isOpen={pendingDelete !== null}
        onClose={() => setPendingDelete(null)}
        onConfirm={() => {
          if (pendingDelete) deleteMutation.mutate(pendingDelete.id);
          setPendingDelete(null);
        }}
        title="Delete tariff"
        description={
          pendingDelete
            ? `Permanently delete "${pendingDelete.name}"? This cannot be undone. Tariffs with payment history are protected — you'll be told to archive instead.`
            : ''
        }
        confirmText="Delete"
        confirmVariant="danger"
        isLoading={deleteMutation.isPending}
      />

      <ConfirmationModal
        isOpen={pendingArchive !== null}
        onClose={() => setPendingArchive(null)}
        onConfirm={() => {
          if (pendingArchive) archiveMutation.mutate(pendingArchive.id);
          setPendingArchive(null);
        }}
        title="Archive tariff"
        description={
          pendingArchive
            ? `Archive "${pendingArchive.name}"? It disappears from the bot catalog and new purchases stop. Existing subscribers keep their access. You can restore it anytime.`
            : ''
        }
        confirmText="Archive"
        confirmVariant="danger"
        isLoading={archiveMutation.isPending}
      />
    </div>
  );
}

function formatRub(amount: number): string {
  if (amount >= 1_000_000) return `${(amount / 1_000_000).toFixed(1)}M ₽`;
  if (amount >= 1_000) return `${(amount / 1_000).toFixed(1)}K ₽`;
  return `${amount} ₽`;
}
