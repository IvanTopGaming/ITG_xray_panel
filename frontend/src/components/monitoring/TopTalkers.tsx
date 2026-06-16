import { useSnapshot } from './useSnapshot';
import { fmtRate } from './format';

export function TopTalkers() {
  const { data: snap } = useSnapshot();

  const talkers = (snap?.series ?? [])
    .filter((s) => s.scope === 'vpn' && s.metric === 'vpn_down')
    .sort((a, b) => b.value - a.value)
    .slice(0, 8);

  const total = talkers.reduce((acc, s) => acc + s.value, 0);

  return (
    <div className="bg-white/[0.04] rounded-2xl border border-white/[0.05] p-4 flex flex-col gap-3 min-w-0">
      <span className="text-sm font-medium text-white/70">Top talkers</span>
      {talkers.length === 0 ? (
        <div className="text-sm text-white/30 py-6 text-center">No activity</div>
      ) : (
        <div className="flex flex-col gap-2">
          {talkers.map((s) => {
            const pct = total > 0 ? (s.value / total) * 100 : 0;
            return (
              <div key={`${s.scope}-${s.entity}`} className="flex flex-col gap-1 min-w-0">
                <div className="flex items-center justify-between gap-2 min-w-0">
                  <span className="text-sm text-white/80 truncate">{s.name || s.entity}</span>
                  <span className="text-xs text-white/60 font-medium whitespace-nowrap">
                    {fmtRate(s.value)}
                  </span>
                </div>
                <div className="h-1.5 rounded-full bg-white/[0.05] overflow-hidden">
                  <div
                    className="h-full rounded-full bg-gradient-to-r from-primary/60 to-violet-600/50"
                    style={{ width: `${pct.toFixed(1)}%` }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}
      <span className="text-[11px] text-white/30 mt-1">≈ best-effort</span>
    </div>
  );
}
