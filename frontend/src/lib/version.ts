import api from './api';

export interface VersionInfo {
  running: {
    backend: string | null;
    bot: string | null;
    bot_reported_at: number | null;
  };
  latest: Record<string, string> | null;
  latest_checked_at: number | null;
}

export async function getVersionInfo(): Promise<VersionInfo> {
  return (await api.get<VersionInfo>('/system/version')).data;
}

function parseVer(v: string): number[] {
  return v
    .replace(/^v/, '')
    .split('.')
    .map((n) => parseInt(n, 10) || 0);
}

/** True when `latest` is strictly newer than `current`. Safe on dev/empty. */
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
