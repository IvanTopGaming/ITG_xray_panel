import { useMemo, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import { Package, ChevronUp, ChevronDown, Infinity as InfinityIcon } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { Tariff, TariffStatsMap } from '@/lib/types';
import { TariffRowMenu } from './TariffRowMenu';
import { parseDate } from '@/lib/datetime';

type SortKey = 'id' | 'name' | 'price' | 'period' | 'active' | 'revenue' | 'last_sale';
type SortDir = 'asc' | 'desc';

interface TariffsTableProps {
  tariffs: Tariff[];
  stats: TariffStatsMap;
  selectedId: number | null;
  onRowClick: (t: Tariff) => void;
  onDuplicate: (t: Tariff) => void;
  onArchive: (t: Tariff) => void;
  onRestore: (t: Tariff) => void;
  onDelete: (t: Tariff) => void;
}

export function TariffsTable({
  tariffs,
  stats,
  selectedId,
  onRowClick,
  onDuplicate,
  onArchive,
  onRestore,
  onDelete,
}: TariffsTableProps) {
  const [sortKey, setSortKey] = useState<SortKey>('active');
  const [sortDir, setSortDir] = useState<SortDir>('desc');

  const sorted = useMemo(() => {
    const copy = [...tariffs];
    copy.sort((a, b) => {
      const va = sortVal(a, stats, sortKey);
      const vb = sortVal(b, stats, sortKey);
      if (va === vb) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      return va < vb ? (sortDir === 'asc' ? -1 : 1) : sortDir === 'asc' ? 1 : -1;
    });
    return copy;
  }, [tariffs, stats, sortKey, sortDir]);

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir(key === 'name' || key === 'period' ? 'asc' : 'desc');
    }
  };

  if (tariffs.length === 0) {
    return (
      <div className="rounded-2xl border border-white/[0.05] bg-white/[0.02] p-10 text-center text-sm text-white/50">
        No tariffs to show.
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-2xl border border-white/[0.05] bg-white/[0.04]">
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="bg-white/[0.02]">
              <SortHeader
                label="ID"
                sortKey="id"
                current={sortKey}
                dir={sortDir}
                onSort={toggleSort}
              />
              <SortHeader
                label="Tariff"
                sortKey="name"
                current={sortKey}
                dir={sortDir}
                onSort={toggleSort}
              />
              <SortHeader
                label="Price"
                sortKey="price"
                current={sortKey}
                dir={sortDir}
                onSort={toggleSort}
                align="right"
              />
              <SortHeader
                label="Period"
                sortKey="period"
                current={sortKey}
                dir={sortDir}
                onSort={toggleSort}
              />
              <th className="px-4 py-3.5 text-left text-xs font-semibold uppercase tracking-wider text-white/50">
                Includes
              </th>
              <SortHeader
                label="Active"
                sortKey="active"
                current={sortKey}
                dir={sortDir}
                onSort={toggleSort}
                align="right"
              />
              <SortHeader
                label="Revenue 30d"
                sortKey="revenue"
                current={sortKey}
                dir={sortDir}
                onSort={toggleSort}
                align="right"
              />
              <SortHeader
                label="Last sale"
                sortKey="last_sale"
                current={sortKey}
                dir={sortDir}
                onSort={toggleSort}
              />
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody>
            <AnimatePresence>
              {sorted.map((t) => (
                <TariffRow
                  key={t.id}
                  tariff={t}
                  stat={stats[t.id]}
                  selected={t.id === selectedId}
                  onClick={() => onRowClick(t)}
                  onDuplicate={() => onDuplicate(t)}
                  onArchive={() => onArchive(t)}
                  onRestore={() => onRestore(t)}
                  onDelete={() => onDelete(t)}
                />
              ))}
            </AnimatePresence>
          </tbody>
        </table>
      </div>
    </div>
  );
}

function sortVal(t: Tariff, stats: TariffStatsMap, key: SortKey): string | number | null {
  const s = stats[t.id];
  switch (key) {
    case 'id':
      return t.id;
    case 'name':
      return t.name.toLowerCase();
    case 'price':
      return t.price_rub;
    case 'period':
      return t.period_days;
    case 'active':
      return s?.active_subs ?? 0;
    case 'revenue':
      return s?.revenue_30d ?? 0;
    case 'last_sale':
      return s?.last_sale_at ?? null;
  }
}

interface SortHeaderProps {
  label: string;
  sortKey: SortKey;
  current: SortKey;
  dir: SortDir;
  onSort: (k: SortKey) => void;
  align?: 'left' | 'right';
}

function SortHeader({ label, sortKey, current, dir, onSort, align = 'left' }: SortHeaderProps) {
  const active = current === sortKey;
  return (
    <th
      className={cn(
        'cursor-pointer select-none px-4 py-3.5 text-xs font-semibold uppercase tracking-wider transition-colors',
        align === 'right' ? 'text-right' : 'text-left',
        active ? 'text-white/85' : 'text-white/40 hover:text-white/60'
      )}
      onClick={() => onSort(sortKey)}
    >
      <span
        className={cn('inline-flex items-center gap-1', align === 'right' && 'flex-row-reverse')}
      >
        {label}
        {active && (dir === 'asc' ? <ChevronUp size={11} /> : <ChevronDown size={11} />)}
      </span>
    </th>
  );
}

interface TariffRowProps {
  tariff: Tariff;
  stat: { active_subs: number; revenue_30d: number; last_sale_at: string | null } | undefined;
  selected: boolean;
  onClick: () => void;
  onDuplicate: () => void;
  onArchive: () => void;
  onRestore: () => void;
  onDelete: () => void;
}

function TariffRow({
  tariff,
  stat,
  selected,
  onClick,
  onDuplicate,
  onArchive,
  onRestore,
  onDelete,
}: TariffRowProps) {
  const activeSubs = stat?.active_subs ?? 0;
  const revenue30d = stat?.revenue_30d ?? 0;
  const lastSaleAt = stat?.last_sale_at ?? null;

  return (
    <motion.tr
      layout="position"
      variants={{
        initial: { opacity: 0, y: 6 },
        animate: {
          opacity: 1,
          y: 0,
          transition: { duration: 0.25, ease: [0.25, 0.46, 0.45, 0.94] },
        },
        exit: {
          opacity: 0,
          x: -20,
          transition: { duration: 0.18, ease: [0.4, 0, 1, 1] },
        },
      }}
      transition={{ layout: { type: 'spring', stiffness: 400, damping: 35 } }}
      initial="initial"
      animate="animate"
      exit="exit"
      onClick={onClick}
      className={cn(
        'group cursor-pointer border-t border-white/[0.04] transition-colors',
        selected ? 'bg-primary/[0.07]' : 'hover:bg-white/[0.02]'
      )}
    >
      <td className="px-4 py-3.5">
        <span className="font-mono text-xs tabular-nums text-white/40">#{tariff.id}</span>
      </td>

      <td className="px-4 py-3.5">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border border-violet-500/20 bg-gradient-to-br from-violet-500/15 to-violet-600/5 text-violet-200">
            <Package size={16} />
          </div>
          <div className="flex min-w-0 flex-col">
            <span className="flex items-center gap-2 truncate text-sm font-semibold text-gray-200">
              {tariff.name}
              {tariff.visibility === 'private' && <Badge tone="indigo">private</Badge>}
              {tariff.visibility === 'archived' && <Badge tone="zinc">archived</Badge>}
              {!tariff.enabled && <Badge tone="rose">disabled</Badge>}
            </span>
          </div>
        </div>
      </td>

      <td className="px-4 py-3.5 text-right">
        <span className="font-mono text-sm font-semibold text-gray-200">{tariff.price_rub}₽</span>
      </td>

      <td className="px-4 py-3.5">
        <span className="font-mono text-xs text-gray-500">{tariff.period_days} d</span>
      </td>

      <td className="px-4 py-3.5">
        <IncludesSummary tariff={tariff} />
      </td>

      <td className="px-4 py-3.5 text-right">
        <span
          className={cn(
            'font-mono text-sm font-semibold tabular-nums',
            activeSubs > 0 ? 'text-violet-300' : 'text-gray-600'
          )}
        >
          {activeSubs}
        </span>
      </td>

      <td className="px-4 py-3.5 text-right">
        <span
          className={cn(
            'font-mono text-sm font-semibold tabular-nums',
            revenue30d > 0 ? 'text-emerald-300' : 'text-gray-600'
          )}
        >
          {revenue30d > 0 ? formatRubShort(revenue30d) : '—'}
        </span>
      </td>

      <td className="px-4 py-3.5">
        <LastSale at={lastSaleAt} />
      </td>

      <td className="px-4 py-3.5">
        <div className="flex justify-end">
          <TariffRowMenu
            tariff={tariff}
            onDuplicate={onDuplicate}
            onArchive={onArchive}
            onRestore={onRestore}
            onDelete={onDelete}
          />
        </div>
      </td>
    </motion.tr>
  );
}

function IncludesSummary({ tariff }: { tariff: Tariff }) {
  if (tariff.items.length === 0) {
    return <span className="text-xs italic text-white/30">no inbounds</span>;
  }
  const hasUnlim = tariff.items.some((i) => i.traffic_gb === 0);
  const totalGb = tariff.items.reduce((acc, i) => acc + i.traffic_gb, 0);
  const panelCount = new Set(tariff.items.map((i) => i.panel_id).filter((id) => id != null)).size;

  return (
    <div className="flex items-center gap-1.5 text-xs">
      <span className="text-gray-300">
        {tariff.items.length} inbound{tariff.items.length === 1 ? '' : 's'}
      </span>
      <span className="text-gray-600">·</span>
      {hasUnlim ? (
        <span className="inline-flex items-center gap-0.5 font-mono text-emerald-300">
          <InfinityIcon size={12} />
          unlimited
        </span>
      ) : (
        <span className="font-mono text-gray-400">{totalGb} GB</span>
      )}
      {panelCount > 0 && (
        <>
          <span className="text-gray-600">·</span>
          <span className="font-mono text-[10px] uppercase tracking-wide text-indigo-300/85">
            {panelCount} panel{panelCount === 1 ? '' : 's'}
          </span>
        </>
      )}
    </div>
  );
}

function LastSale({ at }: { at: string | null }) {
  if (!at) return <span className="text-xs text-gray-600">never</span>;
  const ts = parseDate(at);
  if (!ts) return <span className="text-xs text-gray-600">never</span>;
  const ageDays = (Date.now() - ts.getTime()) / 86_400_000;
  const stale = ageDays >= 3;
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-md border px-1.5 py-0.5 font-mono text-[11px]',
        stale
          ? 'border-rose-500/25 bg-rose-500/10 text-rose-300'
          : 'border-white/[0.06] bg-black/20 text-gray-400'
      )}
    >
      {formatRelative(ts)}
    </span>
  );
}

function Badge({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone: 'indigo' | 'zinc' | 'rose';
}) {
  const tones = {
    indigo: 'border-indigo-500/25 bg-indigo-500/10 text-indigo-300',
    zinc: 'border-zinc-500/25 bg-zinc-500/10 text-zinc-300',
    rose: 'border-rose-500/25 bg-rose-500/10 text-rose-300',
  };
  return (
    <span
      className={cn(
        'rounded-md border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider',
        tones[tone]
      )}
    >
      {children}
    </span>
  );
}

function formatRubShort(amount: number): string {
  if (amount >= 1_000_000) return `${(amount / 1_000_000).toFixed(1)}M₽`;
  if (amount >= 1_000) return `${(amount / 1_000).toFixed(1)}K₽`;
  return `${amount}₽`;
}

function formatRelative(ts: Date): string {
  const diff = Date.now() - ts.getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  return `${months}mo ago`;
}
