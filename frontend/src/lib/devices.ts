// Device tracking helpers for displaying ClientDevice rows in the admin UI.

import { formatDate } from './datetime';

export function deviceIcon(os: string): string {
  const o = (os || '').toLowerCase();
  if (o.includes('ios') || o.includes('android')) return '📱';
  if (o.includes('mac')) return '💻';
  if (o.includes('windows') || o.includes('linux')) return '🖥';
  return '⚙';
}

export function timeAgo(ms: number): string {
  if (!ms) return 'never';
  const diff = Date.now() - ms;
  if (diff < 60_000) return 'just now';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  if (diff < 30 * 86_400_000) return `${Math.floor(diff / 86_400_000)}d ago`;
  return formatDate(ms);
}
