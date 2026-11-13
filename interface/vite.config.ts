import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * Développement : `npm run dev` (port 5173) renvoie /api vers le back end Python (port 8008, interface/run.sh).
 * Production : `npm run build` produit dist/, servi directement par le back end (une seule adresse : http://localhost:8008).
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8008' },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 2048,
  },
});
