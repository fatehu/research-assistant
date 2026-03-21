import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

const watchPolling = process.env.VITE_DEV_WATCH_POLLING === 'true'
const watchInterval = Number(process.env.VITE_DEV_WATCH_POLLING_INTERVAL || 300)
const proxyTarget = process.env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000'
const disableHostNodeModuleAliases = process.env.VITE_DISABLE_HOST_MODULE_ALIASES === 'true'
const useHostNodeModuleAliases =
  !disableHostNodeModuleAliases
  && (
    process.env.VITE_FORCE_HOST_MODULE_ALIASES === 'true'
    || (__dirname.toLowerCase().includes('/mnt/d/') && !__dirname.startsWith('/app'))
  )

// https://vitejs.dev/config/
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
      ...(useHostNodeModuleAliases
        ? [
            {
              find: /^react$/,
              replacement: path.resolve(__dirname, './node_modules/react/index.js'),
            },
            {
              find: /^react-dom$/,
              replacement: path.resolve(__dirname, './node_modules/react-dom/index.js'),
            },
            {
              find: /^react-dom\/client$/,
              replacement: path.resolve(__dirname, './node_modules/react-dom/client.js'),
            },
          ]
        : []),
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
  optimizeDeps: {
    exclude: ['react', 'react-dom', 'react/jsx-runtime', 'react/jsx-dev-runtime'],
  },
  css: {
    preprocessorOptions: {
      less: {
        javascriptEnabled: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor-react': ['react', 'react-dom', 'react-router-dom'],
          'vendor-antd': ['antd', '@ant-design/icons'],
          'vendor-motion': ['framer-motion'],
          'vendor-pdf': ['react-pdf', 'pdfjs-dist'],
        },
      },
    },
  },
})
