import { defineConfig, Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { readFileSync, existsSync } from 'fs';

const adminSrc = path.resolve(__dirname, './src');
const uiCoreSrc = path.resolve(__dirname, '../ui-core/src');
const resolveExtensions = ['', '.tsx', '.ts', '.jsx', '.js', '.css'];

function resolveSharedRoot(rel: string): string | null {
  for (const root of [adminSrc, uiCoreSrc]) {
    for (const ext of resolveExtensions) {
      const candidate = path.join(root, rel + ext);
      if (existsSync(candidate)) return candidate;
    }
  }
  return null;
}

function sharedAliasFallback(): Plugin {
  return {
    name: 'admin-shared-alias-fallback',
    enforce: 'pre',
    resolveId(source) {
      if (!source.startsWith('@/')) return null;
      return resolveSharedRoot(source.slice(2)) ?? null;
    },
  };
}

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

export default defineConfig({
  plugins: [sharedAliasFallback(), react()],
  base: './',
  define: {
    __APP_VERSIONS__: JSON.stringify(versions),
    __FRONTEND_VERSION_KEY__: JSON.stringify('frontend_admin'),
    __EXPECTED_PANEL_ROLE__: JSON.stringify('master'),
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
      '@ui': uiCoreSrc,
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
