import { useSnapshot } from './useSnapshot';
import { fmtBytes, fmtRate, fmtCorePct } from './format';

export function ContainersTable() {
  const { data: snap } = useSnapshot();

  const containers = (() => {
    if (!snap) return [];
    const map = new Map<string, { name: string; cpu?: number; ram?: number; io?: number }>();
    for (const p of snap.series) {
      if (p.scope !== 'container') continue;
      const row = map.get(p.entity) ?? { name: p.name ?? p.entity.slice(0, 12) };
      if (p.metric === 'cpu_ctr') row.cpu = p.value;
      else if (p.metric === 'ram_ctr') row.ram = p.value;
      else if (p.metric === 'io_ctr') row.io = p.value;
      map.set(p.entity, row);
    }
    return [...map.values()].sort((a, b) => (b.ram ?? 0) - (a.ram ?? 0));
  })();

  return (
    <div className="bg-white/[0.04] rounded-2xl border border-white/[0.05] p-4 flex flex-col gap-3 min-w-0">
      <span className="text-sm font-medium text-white/70">Containers</span>
      {containers.length === 0 ? (
        <div className="text-sm text-white/30 py-6 text-center">No data</div>
      ) : (
        <div className="flex flex-col gap-0">
          <div className="grid grid-cols-4 gap-2 px-1 pb-1 border-b border-white/[0.05]">
            <span className="text-[11px] text-white/30 uppercase tracking-wide">Name</span>
            <span className="text-[11px] text-white/30 uppercase tracking-wide text-right">
              CPU
            </span>
            <span className="text-[11px] text-white/30 uppercase tracking-wide text-right">
              RAM
            </span>
            <span className="text-[11px] text-white/30 uppercase tracking-wide text-right">IO</span>
          </div>
          {containers.map((c) => (
            <div
              key={c.name}
              className="grid grid-cols-4 gap-2 px-1 py-1.5 border-b border-white/[0.03] last:border-0"
            >
              <span className="text-sm text-white/80 truncate">{c.name}</span>
              <span className="text-xs text-white/60 text-right font-medium tabular-nums">
                {fmtCorePct(c.cpu)}
              </span>
              <span className="text-xs text-white/60 text-right font-medium tabular-nums">
                {fmtBytes(c.ram)}
              </span>
              <span className="text-xs text-white/60 text-right font-medium tabular-nums">
                {fmtRate(c.io)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
