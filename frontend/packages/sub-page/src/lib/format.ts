import type { Lang } from './i18n';

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB'];

export function formatBytes(n: number): string {
  const value = Number(n) || 0;
  if (value <= 0) return '0 B';
  let f = value;
  let i = 0;
  while (f >= 1024 && i < UNITS.length - 1) {
    f /= 1024;
    i += 1;
  }
  return `${f.toFixed(2)} ${UNITS[i]}`;
}

export function daysLeft(ms: number): number {
  return Math.ceil((ms - Date.now()) / 86400000);
}

export function formatDate(ms: number, lang: Lang, months: readonly string[]): string {
  if (!ms || ms <= 0) return '';
  const d = new Date(ms);
  const month = months[d.getMonth()] ?? '';
  return lang === 'ru' ? `${d.getDate()} ${month}` : `${month} ${d.getDate()}`;
}
