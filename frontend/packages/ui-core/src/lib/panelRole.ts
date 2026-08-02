export type PanelRole = 'master' | 'worker';

export function readInjectedPanelRole(): string {
  const meta = document.querySelector('meta[name="panel-role"]');
  return String(meta?.getAttribute('content') || '')
    .trim()
    .toLowerCase();
}

const rawRole = readInjectedPanelRole();

const ROLES_WITH_LOCAL_XRAY: ReadonlySet<string> = new Set(['worker']);

export const panelRole: PanelRole = rawRole === 'worker' ? 'worker' : 'master';

export const isWorker = panelRole === 'worker';

export const hasLocalXray = ROLES_WITH_LOCAL_XRAY.has(rawRole);
