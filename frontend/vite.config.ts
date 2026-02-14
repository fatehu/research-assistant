import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

const watchPolling = process.env.VITE_DEV_WATCH_POLLING === 'true'
const watchInterval = Number(process.env.VITE_DEV_WATCH_POLLING_INTERVAL || 300)

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 3000,
    host: true,
    watch: watchPolling
      ? {
          usePolling: true,
          interval: Number.isFinite(watchInterval) ? watchInterval : 300,
        }
      : undefined,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  css: {
    preprocessorOptions: {
      less: {
        javascriptEnabled: true,
      },
    },
  },
})
