import { MonitoringSnapshot } from '../../lib/types';

export function pickValue(
  snap: MonitoringSnapshot | undefined,
  metric: string,
  scope: string,
  entity?: string
): number | undefined {
  if (!snap) return undefined;
  const point = snap.series.find(
    (p) => p.metric === metric && p.scope === scope && (entity === undefined || p.entity === entity)
  );
  return point?.value;
}

export function fmtBytes(n: number | undefined): string {
  if (n === undefined) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + ' GB';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + ' MB';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + ' KB';
  return n.toFixed(0) + ' B';
}

export function fmtRate(n: number | undefined): string {
  if (n === undefined) return '—';
  return fmtBytes(n) + '/s';
}

export function fmtPct(hundredths: number | undefined): string {
  if (hundredths === undefined) return '—';
  return (hundredths / 100).toFixed(0) + '%';
}

export function fmtCorePct(usecPerSec: number | undefined): string {
  if (usecPerSec === undefined) return '—';
  return (usecPerSec / 10000).toFixed(0) + '%';
}
