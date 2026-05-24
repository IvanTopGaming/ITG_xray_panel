import { Gift, Plus } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { Tariff, TariffStats } from '@/lib/types';

interface TrialCardProps {
  trial: Tariff | null;
  stats: TariffStats | null;
  onEdit: (t: Tariff) => void;
  onCreate: () => void;
}

export function TrialCard({ trial, stats, onEdit, onCreate }: TrialCardProps) {
  if (!trial) {
    return (
      <button
        type="button"
        onClick={onCreate}
        className={cn(
          'group flex w-full items-center gap-4 rounded-2xl border border-dashed border-amber-500/20',
          'bg-gradient-to-br from-amber-500/[0.04] to-transparent p-5 text-left transition-colors',
          'hover:border-amber-500/40 hover:bg-amber-500/[0.06]'
        )}
      >
        <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-amber-500/30 bg-amber-500/15 text-amber-300">
          <Gift size={18} />
        </div>
        <div className="flex-1">
          <div className="text-sm font-semibold text-white/90">No trial tariff configured</div>
          <div className="mt-0.5 text-xs text-white/50">
            New users will not be offered a free trial. Click to create one.
          </div>
        </div>
        <div className="flex items-center gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/15 px-3 py-1.5 text-xs font-semibold text-amber-200 group-hover:bg-amber-500/25">
          <Plus size={12} />
          Set up trial
        </div>
      </button>
    );
  }

  const includesSummary = (() => {
    if (trial.items.length === 0) return 'No inbounds';
    const totalGb = trial.items.reduce((acc, it) => acc + it.traffic_gb, 0);
    const gbLabel = trial.items.some((it) => it.traffic_gb === 0) ? '∞' : `${totalGb} GB`;
    return `${trial.items.length} inbound${trial.items.length === 1 ? '' : 's'} · ${gbLabel}`;
  })();

  return (
    <button
      type="button"
      onClick={() => onEdit(trial)}
      className={cn(
        'group flex w-full items-center gap-4 rounded-2xl border border-amber-500/25',
        'bg-gradient-to-br from-amber-500/[0.10] via-amber-500/[0.04] to-transparent p-5 text-left transition-colors',
        'hover:border-amber-500/40 hover:bg-amber-500/[0.12]'
      )}
    >
      <div className="flex h-11 w-11 items-center justify-center rounded-xl border border-amber-500/30 bg-amber-500/15 text-amber-300">
        <Gift size={18} />
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2">
          <span className="text-sm font-semibold text-white/90 truncate">{trial.name}</span>
          {!trial.enabled && (
            <span className="rounded-md border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-wider text-rose-300">
              disabled
            </span>
          )}
        </div>
        <div className="mt-0.5 text-xs text-white/50">
          {trial.period_days}d · {includesSummary}
        </div>
      </div>
      <div className="flex flex-col items-end gap-0.5 pr-2 text-right">
        <span className="font-mono text-base font-semibold text-violet-300">
          {stats?.active_subs ?? 0}
        </span>
        <span className="text-[9px] font-semibold uppercase tracking-wider text-white/40">
          Active uses
        </span>
      </div>
      <span className="rounded-lg bg-white/[0.06] px-3 py-1.5 text-xs font-medium text-white/70 group-hover:bg-white/[0.10] group-hover:text-white">
        Configure
      </span>
    </button>
  );
}
