import { useSnapshot } from './useSnapshot';
import { fmtBytes, fmtPct } from './format';

export function ProcessesTable() {
  const { data: snap } = useSnapshot();

  const procs = (snap?.procs ?? []).slice(0, 15);

  return (
    <div className="bg-white/[0.04] rounded-2xl border border-white/[0.05] p-4 flex flex-col gap-3 min-w-0">
      <span className="text-sm font-medium text-white/70">Processes</span>
      {procs.length === 0 ? (
        <div className="text-sm text-white/30 py-6 text-center">No data</div>
      ) : (
        <div className="flex flex-col gap-0">
          <div className="grid grid-cols-3 gap-2 px-1 pb-1 border-b border-white/[0.05]">
            <span className="text-[11px] text-white/30 uppercase tracking-wide">Process</span>
            <span className="text-[11px] text-white/30 uppercase tracking-wide text-right">
              CPU
            </span>
            <span className="text-[11px] text-white/30 uppercase tracking-wide text-right">
              RAM
            </span>
          </div>
          {procs.map((p) => (
            <div
              key={p.pid}
              className="grid grid-cols-3 gap-2 px-1 py-1.5 border-b border-white/[0.03] last:border-0"
            >
              <span className="text-sm text-white/80 truncate">{p.comm}</span>
              <span className="text-xs text-white/60 text-right font-medium tabular-nums">
                {fmtPct(p.cpu_pct)}
              </span>
              <span className="text-xs text-white/60 text-right font-medium tabular-nums">
                {fmtBytes(p.rss_bytes)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
