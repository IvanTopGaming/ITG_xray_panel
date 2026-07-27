import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { readFileSync, existsSync } from 'fs';

const versionsCandidates = [
  path.resolve(__dirname, '../../../versions.json'),
  path.resolve(__dirname, '../../versions.json'),
];
const versionsPath = versionsCandidates.find((p) => existsSync(p));
const versions = versionsPath
  ? JSON.parse(readFileSync(versionsPath, 'utf-8'))
  : {
      master: 'dev',
      worker: 'dev',
      sub: 'dev',
      bot_api: 'dev',
      frontend_admin: 'dev',
      frontend_node: 'dev',
      bot: 'dev',
      caddy: 'dev',
      xray_core_ref: 'dev',
    };

const expectedPanelRole = 'master';

function devPanelRoleInjection() {
  return {
    name: 'dev-panel-role-injection',
    apply: 'serve' as const,
    transformIndexHtml(html: string) {
      return html.replace(
        "window.__PANEL_ROLE__ = '__PANEL_ROLE__'",
        `window.__PANEL_ROLE__ = '${expectedPanelRole}'`
      );
    },
  };
}

export default defineConfig({
  plugins: [react(), devPanelRoleInjection()],
  base: './',
  define: {
    __APP_VERSIONS__: JSON.stringify(versions),
    __FRONTEND_VERSION_KEY__: JSON.stringify('frontend_admin'),
    __EXPECTED_PANEL_ROLE__: JSON.stringify(expectedPanelRole),
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          query: ['@tanstack/react-query'],
          motion: ['framer-motion'],
          icons: ['lucide-react'],
        },
      },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '@ui': path.resolve(__dirname, '../ui-core/src'),
    },
  },
  server: {
    port: 4200,
    proxy: {
      '/api': {
        target: process.env.VITE_BACKEND_URL || 'http://backend:5000',
        changeOrigin: true,
      },
    },
  },
});
