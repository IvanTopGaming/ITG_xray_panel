import api from './api';

export interface RoleReport {
  version: string;
  reported_at: number;
}

export interface VersionInfo {
  running: {
    backend: string | null;
    backend_key: string | null;
    bot: string | null;
    bot_reported_at: number | null;
    roles?: Record<string, RoleReport>;
  };
  latest: Record<string, string> | null;
  latest_checked_at: number | null;
}

export interface SystemHealth {
  certificate:
    | { available: true; not_after_ms: number; domains: string[] }
    | { available: false; reason: string };
  undelivered_events: { available: boolean; count?: number };
  stuck_payments: { available: boolean; processing?: number; pending_over_a_day?: number };
  data_tier: { database: string; shared_redis: string };
}

export async function getVersionInfo(): Promise<VersionInfo> {
  return (await api.get<VersionInfo>('/system/version')).data;
}

export async function getSystemHealth(): Promise<SystemHealth> {
  return (await api.get<SystemHealth>('/system/health')).data;
}

function parseVer(v: string): number[] {
  return v
    .replace(/^v/, '')
    .split('.')
    .map((n) => parseInt(n, 10) || 0);
}

export function isNewer(
  latest: string | null | undefined,
  current: string | null | undefined
): boolean {
  if (!latest || !current || current === 'dev' || latest === 'dev') return false;
  const a = parseVer(latest);
  const b = parseVer(current);
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] || 0;
    const y = b[i] || 0;
    if (x !== y) return x > y;
  }
  return false;
}
