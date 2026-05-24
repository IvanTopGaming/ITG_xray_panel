import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import { readFileSync, existsSync } from 'fs';

const versionsCandidates = [
  path.resolve(__dirname, 'versions.json'),
  path.resolve(__dirname, '../versions.json'),
];
const versionsPath = versionsCandidates.find((p) => existsSync(p));
const versions = versionsPath
  ? JSON.parse(readFileSync(versionsPath, 'utf-8'))
  : { backend: 'dev', frontend: 'dev', bot: 'dev', caddy: 'dev', xray_core_ref: 'dev' };

export default defineConfig({
  plugins: [react()],
  base: './',
  define: {
    __APP_VERSIONS__: JSON.stringify(versions),
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
    },
  },
  server: {
    port: 4200,
    proxy: {
      '/api': {
        target: process.env.VITE_BACKEND_URL || 'http://backend:5000',
        changeOrigin: true,
      }
    }
  }
});
