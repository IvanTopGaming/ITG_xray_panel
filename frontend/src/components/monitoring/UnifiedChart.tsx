import { useState, useEffect } from 'react';
import { useSeries } from './useSeries';
import { Chart, ChartSeries } from './Chart';
import { MetricToggles, MetricDef } from './MetricToggles';
import { fmtBytes, fmtRate, fmtPct } from './format';
import { formatWith } from '@/lib/datetime';

const METRICS: MetricDef[] = [
  {
    key: 'cpu',
    label: 'CPU',
    color: '#7fb2dc',
    scope: 'host',
    metric: 'cpu_host',
    entity: '',
    fmt: fmtPct,
  },
  {
    key: 'ram',
    label: 'RAM',
    color: '#d0bcff',
    scope: 'host',
    metric: 'ram_host',
    entity: '',
    fmt: fmtBytes,
  },
  {
    key: 'rx',
    label: 'Net ↓',
    color: '#7fdca4',
    scope: 'host',
    metric: 'net_host_rx',
    entity: '',
    fmt: fmtRate,
  },
  {
    key: 'tx',
    label: 'Net ↑',
    color: '#e0a07f',
    scope: 'host',
    metric: 'net_host_tx',
    entity: '',
    fmt: fmtRate,
  },
  {
    key: 'dr',
    label: 'Disk R',
    color: '#dcc97f',
    scope: 'disk',
    metric: 'disk_io',
    entity: 'read',
    fmt: fmtRate,
  },
  {
    key: 'dw',
    label: 'Disk W',
    color: '#dc7fb2',
    scope: 'disk',
    metric: 'disk_io',
    entity: 'write',
    fmt: fmtRate,
  },
];

export function UnifiedChart({ range }: { range: string }) {
  const [active, setActive] = useState<Set<string>>(new Set(['cpu', 'rx']));
  const [zoom, setZoom] = useState<{ from: number; to: number } | null>(null);

  useEffect(() => setZoom(null), [range]);

  const override = zoom ?? undefined;

  const queries = [
    useSeries('cpu_host', 'host', '', range, active.has('cpu'), override),
    useSeries('ram_host', 'host', '', range, active.has('ram'), override),
    useSeries('net_host_rx', 'host', '', range, active.has('rx'), override),
    useSeries('net_host_tx', 'host', '', range, active.has('tx'), override),
    useSeries('disk_io', 'disk', 'read', range, active.has('dr'), override),
    useSeries('disk_io', 'disk', 'write', range, active.has('dw'), override),
  ];

  const toggle = (key: string) =>
    setActive((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const activeEntries = METRICS.map((m, i) => ({ m, points: queries[i].data ?? [] })).filter(
    ({ m }) => active.has(m.key)
  );

  const series: ChartSeries[] = activeEntries.map(({ m, points }) => ({
    label: m.label,
    color: m.color,
    points,
    fmt: m.fmt,
  }));

  return (
    <div className="bg-white/[0.04] rounded-2xl border border-white/[0.05] p-4 flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2.5">
          <span className="text-sm font-medium text-white/70">Host metrics</span>
          {zoom && (
            <button
              type="button"
              onClick={() => setZoom(null)}
              className="flex items-center gap-1.5 rounded-full bg-white/[0.06] hover:bg-white/[0.1] border border-white/[0.08] px-2.5 py-1 text-[11px] text-white/60 hover:text-white/85 transition-colors"
            >
              <span>Reset zoom</span>
              <span className="text-white/35">
                {formatWith(zoom.from * 1000, {
                  month: 'short',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                  hour12: false,
                })}{' '}
                –{' '}
                {formatWith(zoom.to * 1000, {
                  month: 'short',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                  hour12: false,
                })}
              </span>
            </button>
          )}
        </div>
        <MetricToggles metrics={METRICS} active={active} onToggle={toggle} />
      </div>

      <Chart
        series={series}
        normalizePerSeries
        height={240}
        onZoom={
          range === 'live'
            ? undefined
            : (from, to) => setZoom(to - from < 60 ? { from, to: from + 60 } : { from, to })
        }
      />

      {activeEntries.length > 0 && (
        <div className="flex flex-wrap gap-x-5 gap-y-2 pt-1">
          {activeEntries.map(({ m, points }) => {
            const current = points.length ? points[points.length - 1].value : undefined;
            const peak = points.length ? Math.max(...points.map((p) => p.value)) : undefined;
            return (
              <div key={m.key} className="flex items-center gap-2 text-xs">
                <span
                  className="inline-block w-2 h-2 rounded-full"
                  style={{ backgroundColor: m.color }}
                />
                <span className="text-white/60">{m.label}</span>
                <span className="text-white/80 font-medium">{m.fmt(current)}</span>
                <span className="text-white/35">peak {m.fmt(peak)}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
