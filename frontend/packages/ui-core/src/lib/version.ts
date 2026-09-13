import api from './api';

export interface RoleReport {
  version: string;
  reported_at: number;
  state?: 'reporting' | 'silent';
}

export interface VersionInfo {
  running: {
    backend: string | null;
    backend_key: string | null;
    bot: string | null;
    bot_reported_at: number | null;
    bot_state?: 'reporting' | 'silent' | null;
    roles?: Record<string, RoleReport>;
    superseded_at?: number | null;
  };
  latest: Record<string, string> | null;
  latest_checked_at: number | null;
}

export interface OffsiteBackupReading {
  applicable: boolean;
  available?: boolean;
  last_success_at_ms?: number | null;
  age_seconds?: number | null;
  interval_seconds?: number | null;
  stale_after_seconds?: number;
  remote?: string | null;
  stale?: boolean;
}

export interface SystemHealth {
  jobs?: {
    available: boolean;
    error?: string;
    needs_attention: boolean;
    items: {
      role: string;
      job_id: string;
      interval_s: number;
      status: 'waiting' | 'overdue' | 'running' | 'succeeded' | 'failed';
      registered_at_ms: number;
      started_at_ms: number | null;
      finished_at_ms: number | null;
      last_success_at_ms: number | null;
      last_failure_at_ms: number | null;
      last_error: string | null;
      failures: number;
      stale: boolean;
    }[];
  };
  undelivered_events: { available: boolean; count?: number };
  event_delivery?: {
    available: boolean;
    pending?: number;
    leased?: number;
    review?: number;
    permanent?: number;
    oldest_pending_ms?: number | null;
    needs_attention?: boolean;
  };
  stuck_payments: {
    available: boolean;
    processing?: number;
    pending_over_a_day?: number;
    pending_fulfillment?: number;
    pending_refunds?: number;
    review?: number;
  };
  data_tier: { database: string; shared_redis: string };
  offsite_backup: OffsiteBackupReading;
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
