declare global {
  interface Window {
    __PANEL_ROLE__?: string;
  }
}

export type PanelRole = 'master' | 'worker';

const rawRole = String(window.__PANEL_ROLE__ || '')
  .trim()
  .toLowerCase();

const ROLES_WITH_LOCAL_XRAY: ReadonlySet<string> = new Set(['worker']);

export const panelRole: PanelRole = rawRole === 'worker' ? 'worker' : 'master';

export const isWorker = panelRole === 'worker';

export const hasLocalXray = ROLES_WITH_LOCAL_XRAY.has(rawRole);
