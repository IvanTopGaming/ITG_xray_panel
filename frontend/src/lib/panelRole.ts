declare global {
  interface Window {
    __PANEL_ROLE__?: string;
  }
}

export type PanelRole = 'master' | 'worker';

export const panelRole: PanelRole = window.__PANEL_ROLE__ === 'worker' ? 'worker' : 'master';

export const isWorker = panelRole === 'worker';
