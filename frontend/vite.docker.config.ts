import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

const watchPolling = process.env.VITE_DEV_WATCH_POLLING === 'true'
const watchInterval = Number(process.env.VITE_DEV_WATCH_POLLING_INTERVAL || 300)
const proxyTarget = process.env.VITE_PROXY_TARGET || 'http://backend:8000'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: [
      {
        find: '@',
        replacement: path.resolve(__dirname, './src'),
      },
      {
        find: /^react\/jsx-runtime$/,
        replacement: path.resolve(__dirname, './node_modules/react/jsx-runtime.js'),
      },
      {
        find: /^react\/jsx-dev-runtime$/,
        replacement: path.resolve(__dirname, './node_modules/react/jsx-dev-runtime.js'),
      },
      {
        find: /^react-dom\/client$/,
        replacement: path.resolve(__dirname, './node_modules/react-dom/client.js'),
      },
    ],
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
        target: proxyTarget,
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
